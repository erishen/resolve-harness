"""Tests for the deterministic fast path (no LLM involved)."""

from __future__ import annotations

import pytest

from agentpulse.fastpath import try_fast_answer


@pytest.fixture(autouse=True)
def _isolate_plugins(tmp_path, monkeypatch):
    """Point the plugin dir at a temp (empty) folder so real runtime-generated
    plugins can't leak into these unit tests, and drop promoted source
    detectors too (they may grow to match arbitrary inputs)."""
    from agentpulse import codegen, fastpath

    monkeypatch.setattr(codegen, "default_plugin_dir", lambda: tmp_path / "empty-plugins")
    monkeypatch.setattr(fastpath, "_GENERATED_DETECTORS", [])


class TestArithmetic:
    @pytest.mark.parametrize(
        "query,expected_value",
        [
            ("计算 2+3", "5"),
            ("2+3=?", "5"),
            ("23 × 45 是多少", "1035"),
            ("7*8", "56"),
            ("计算 (23+45) 和 (67+89)", "68"),
            ("100 - 37", "63"),
            ("3.5 + 1.5", "5"),
            ("12÷4", "3"),
            ("2 加 3", "5"),
            ("10 减 4", "6"),
            ("8 乘 9", "72"),
            ("2^10", "1024"),
        ],
    )
    def test_simple(self, query: str, expected_value: str) -> None:
        result = try_fast_answer(query)
        assert result is not None
        assert result.method == "arithmetic"
        assert f"= {expected_value}" in result.answer

    def test_multi_expression_comparison(self) -> None:
        result = try_fast_answer("计算 (23+45) 和 (67+89)，并告诉我哪个结果更大")
        assert result is not None
        assert "(23+45) = 68" in result.answer
        assert "(67+89) = 156" in result.answer
        assert "更大" in result.answer

    def test_equal_values_no_bias(self) -> None:
        result = try_fast_answer("2+1 和 1+2 哪个更大")
        assert result is not None
        assert "相等" in result.answer

    def test_detail_contains_computation(self) -> None:
        result = try_fast_answer("计算 2+3")
        assert "2+3 = 5" in result.detail


class TestStatistics:
    def test_max(self) -> None:
        result = try_fast_answer("3、1、5 的最大值")
        assert result is not None and result.method == "statistics"
        assert "5" in result.answer

    def test_min(self) -> None:
        result = try_fast_answer("这些数里最小的是 8, 3, 12")
        assert result is not None
        assert "3" in result.answer

    def test_average(self) -> None:
        result = try_fast_answer("1, 2, 3 的平均值")
        assert result is not None
        assert "2" in result.answer

    def test_sum(self) -> None:
        result = try_fast_answer("10 和 20 和 30 的总和")
        assert result is not None
        assert "60" in result.answer

    def test_sort(self) -> None:
        result = try_fast_answer("把 3, 1, 2 从小到大排序")
        assert result is not None
        assert result.answer.count("、") >= 2
        assert "1" in result.answer and "2" in result.answer and "3" in result.answer

    def test_single_number_not_enough(self) -> None:
        assert try_fast_answer("5 的最大值") is None


class TestUnitConvert:
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("100 华氏度等于多少摄氏度", "37.78"),
            ("100 摄氏度等于多少华氏度", "212"),
            ("5 公里等于多少英里", "3.11"),
            ("10 英里等于多少公里", "16.09"),
            ("2 千克等于多少磅", "4.41"),
            ("3 斤等于多少千克", "1.5"),
            ("2 小时等于多少分钟", "120"),
            ("90 分钟等于多少小时", "1.5"),
        ],
    )
    def test_conversions(self, query: str, expected: str) -> None:
        result = try_fast_answer(query)
        assert result is not None, query
        assert result.method == "unit_convert"
        assert expected in result.answer


class TestDateMath:
    def test_tomorrow(self) -> None:
        result = try_fast_answer("明天是几号")
        assert result is not None and result.method == "date_math"
        assert "明天是" in result.answer

    def test_days_later(self) -> None:
        result = try_fast_answer("3 天后是哪天")
        assert result is not None
        assert "3 天后是" in result.answer

    def test_date_diff(self) -> None:
        result = try_fast_answer("2026-01-01 和 2026-01-10 相差几天")
        assert result is not None
        assert "9 天" in result.answer


class TestBaseConvert:
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("255 的十六进制", "ff"),
            ("17 的二进制", "10001"),
            ("8 的八进制", "10"),
        ],
    )
    def test_base(self, query: str, expected: str) -> None:
        result = try_fast_answer(query)
        assert result is not None, query
        assert result.method == "base_convert"
        assert expected in result.answer


class TestTextStats:
    def test_char_count(self) -> None:
        result = try_fast_answer("「你好世界」有几个字")
        assert result is not None and result.method == "text_stats"
        assert "4" in result.answer


class TestSandbox:
    def test_list_files(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.md").write_text("y", encoding="utf-8")
        result = try_fast_answer("沙箱里有哪些文件", sandbox_dir=str(tmp_path))
        assert result is not None and result.method == "sandbox_files"
        assert "a.txt" in result.answer and "sub/b.md" in result.answer

    def test_read_file(self, tmp_path) -> None:
        (tmp_path / "hello.txt").write_text("你好 fast path", encoding="utf-8")
        result = try_fast_answer("读取沙箱里的 hello.txt", sandbox_dir=str(tmp_path))
        assert result is not None
        assert "你好 fast path" in result.answer

    def test_missing_sandbox_file(self, tmp_path) -> None:
        result = try_fast_answer("读取沙箱里的 nope.txt", sandbox_dir=str(tmp_path))
        assert result is not None
        assert "不存在" in result.answer

    def test_no_sandbox_dir_no_match(self) -> None:
        assert try_fast_answer("沙箱里有哪些文件") is None


class TestTime:
    def test_current_time(self) -> None:
        result = try_fast_answer("现在几点")
        assert result is not None
        assert result.method == "time"
        assert "现在是" in result.answer

    def test_english_time(self) -> None:
        result = try_fast_answer("what time is it now?")
        assert result is not None
        assert "现在是" in result.answer


class TestNonMatches:
    @pytest.mark.parametrize(
        "query",
        [
            "写一份 RAG 技术简介",
            "你好",
            "记住我喜欢深色主题",
            "解释一下什么是 agent",
            "",
            "   ",
        ],
    )
    def test_returns_none(self, query: str) -> None:
        assert try_fast_answer(query) is None


class TestSafety:
    @pytest.mark.parametrize(
        "evil",
        [
            "计算 __import__('os').system('ls')",
            "2+3; import os",
            "计算 [1,2,3]",
            "open('/etc/passwd')",
        ],
    )
    def test_unsafe_input_never_executes(self, evil: str) -> None:
        # must either return None or a pure math result — never crash/execute
        result = try_fast_answer(evil)
        if result is not None:
            assert result.method == "arithmetic"

    def test_division_by_zero_returns_none(self) -> None:
        result = try_fast_answer("计算 1/0")
        # expr extraction skips unparseable-at-eval expressions
        assert result is None or "inf" not in result.answer.lower()
