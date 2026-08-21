"""Tests for the example-task service (fixed built-in set + regeneration)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentpulse.examples import (
    BUILTIN_EXAMPLES,
    delete_example,
    generate_examples,
    generate_fresh_examples,
)


class _FakeHarness:
    """Stand-in that returns a controllable model response for regeneration."""

    def __init__(self, content: str) -> None:
        self.sandbox_dir = None
        self.router = SimpleNamespace(
            complete=lambda *a, **k: {"role": "assistant", "content": content}
        )


@pytest.fixture(autouse=True)
def isolated_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the tombstone store at a temp file and reset the module cache."""
    target = tmp_path / "deleted_examples.json"
    monkeypatch.setattr("agentpulse.examples._deleted_file", lambda: target)
    monkeypatch.setattr("agentpulse.examples._cache", None)
    return target


class TestExamples:
    def test_returns_builtins(self) -> None:
        result = generate_examples()
        assert result == BUILTIN_EXAMPLES
        # the memory demo task is intentionally absent from the grid
        assert not any("记住" in e["text"] for e in result)

    def test_returns_a_copy(self) -> None:
        result = generate_examples()
        result.append({"label": "X", "text": "Y", "source": "builtin"})
        assert generate_examples() == BUILTIN_EXAMPLES  # caller can't mutate the source

    def test_every_item_has_source(self) -> None:
        assert all(e.get("source") == "builtin" for e in generate_examples())


class TestRegenerate:
    def test_builtins_plus_generated(self) -> None:
        content = (
            '[{"label": "数据清洗", "text": "生成 12 个带噪声的温度读数保存为 temps.csv，'
            '写脚本清洗异常值并输出均值与最大值"}]'
        )
        result = generate_fresh_examples(_FakeHarness(content), force=True)
        assert result[: len(BUILTIN_EXAMPLES)] == BUILTIN_EXAMPLES
        generated = [e for e in result if e.get("source") == "generated"]
        assert len(generated) == 1
        assert generated[0]["label"] == "数据清洗"

    def test_rejects_root_path_and_english_label(self) -> None:
        content = (
            '[{"label": "改配置", "text": "将 /app/config/editor.json 的 theme 改为 dark"},'
            '{"label": "cleanData", "text": "清洗一份 csv 数据并输出统计"}]'
        )
        result = generate_fresh_examples(_FakeHarness(content), force=True)
        generated = [e for e in result if e.get("source") == "generated"]
        assert generated == []  # both landmines filtered out

    def test_falls_back_to_builtins_on_garbage(self) -> None:
        result = generate_fresh_examples(_FakeHarness("not json at all"), force=True)
        assert result == BUILTIN_EXAMPLES

    def test_force_bypasses_cache(self) -> None:
        first = generate_fresh_examples(_FakeHarness("[]"), force=True)
        # without force, the same (cached) result is returned
        second = generate_fresh_examples(_FakeHarness("[]"))
        assert second is first or second == first


class TestDeleteExamples:
    def _builtin(self) -> dict[str, str]:
        return dict(BUILTIN_EXAMPLES[0])  # 计算：计算 12 × 34 是多少

    def test_delete_builtin_hides_it(self) -> None:
        ex = self._builtin()
        assert delete_example(ex["label"], ex["text"]) is True
        result = generate_examples()
        assert ex["text"] not in [e["text"] for e in result]
        assert len(result) == len(BUILTIN_EXAMPLES) - 1

    def test_delete_is_idempotent(self, isolated_deleted: Path) -> None:
        ex = self._builtin()
        assert delete_example(ex["label"], ex["text"]) is True
        assert delete_example(ex["label"], ex["text"]) is False  # already deleted
        data = isolated_deleted.read_text(encoding="utf-8")
        assert data.count(ex["text"]) == 1  # single tombstone, no duplicates

    def test_delete_persists_across_calls(self) -> None:
        ex = self._builtin()
        delete_example(ex["label"], ex["text"])
        # a fresh call re-reads the file — the example stays hidden
        assert ex["text"] not in [e["text"] for e in generate_examples()]

    def test_delete_rejects_empty_text(self) -> None:
        assert delete_example("x", "   ") is False

    def test_fresh_excludes_deleted_builtin(self) -> None:
        ex = self._builtin()
        delete_example(ex["label"], ex["text"])
        result = generate_fresh_examples(_FakeHarness("[]"), force=True)
        assert ex["text"] not in [e["text"] for e in result]

    def test_regenerate_prompt_carries_negative_list(self) -> None:
        ex = self._builtin()
        delete_example(ex["label"], ex["text"])
        captured: dict[str, str] = {}

        def complete(messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return {"role": "assistant", "content": "[]"}

        h = _FakeHarness("[]")
        h.router = SimpleNamespace(complete=complete)
        generate_fresh_examples(h, force=True)
        assert "已被用户删除" in captured["prompt"]
        assert ex["label"] in captured["prompt"]

    def test_regenerate_filters_deleted_generated(self) -> None:
        deleted = "生成 5 个随机数并计算均值，保存为 nums.txt"
        delete_example("随机数", deleted)
        content = f'[{{"label": "随机数", "text": "{deleted}"}}]'
        result = generate_fresh_examples(_FakeHarness(content), force=True)
        assert deleted not in [e["text"] for e in result]

    def test_delete_invalidates_regeneration_cache(self) -> None:
        deleted = "生成 5 个随机数并计算均值，保存为 nums.txt"
        content = f'[{{"label": "随机数", "text": "{deleted}"}}]'
        first = generate_fresh_examples(_FakeHarness(content), force=True)
        assert any(e["text"] == deleted for e in first)
        delete_example("随机数", deleted)
        # cached result must not resurface the deleted generated example
        second = generate_fresh_examples(_FakeHarness("[]"))
        assert deleted not in [e["text"] for e in second]

