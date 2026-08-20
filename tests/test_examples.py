"""Tests for the example-task service (fixed built-in set + regeneration)."""

from __future__ import annotations

from types import SimpleNamespace

from agentpulse.examples import BUILTIN_EXAMPLES, generate_examples, generate_fresh_examples


class _FakeHarness:
    """Stand-in that returns a controllable model response for regeneration."""

    def __init__(self, content: str) -> None:
        self.sandbox_dir = None
        self.router = SimpleNamespace(
            complete=lambda *a, **k: {"role": "assistant", "content": content}
        )


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

