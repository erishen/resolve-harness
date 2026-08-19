"""Tests for the deterministic fast path (no LLM involved)."""

from __future__ import annotations

import pytest

from agentpulse.fastpath import try_fast_answer


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
