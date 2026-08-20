"""Tests for the example-task service (built-in + memory-personalized)."""

from __future__ import annotations

import pytest

from agentpulse.examples import BUILTIN_EXAMPLES, generate_examples
from agentpulse.harness import Harness


class Router:
    """Returns a canned JSON array of personalized examples."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        return {"role": "assistant", "content": self.payload}


@pytest.fixture(autouse=True)
def _clear_cache():
    from agentpulse import examples

    examples._cache = None
    yield
    examples._cache = None


class TestExamples:
    def test_builtin_always_present(self) -> None:
        h = Harness(memory_db=":memory:")
        result = generate_examples(h)
        assert len(result) >= len(BUILTIN_EXAMPLES)
        # built-ins come first
        assert result[0]["text"] == BUILTIN_EXAMPLES[0]["text"]

    def test_no_memory_skips_llm(self) -> None:
        h = Harness(memory_db=":memory:")
        h.router = Router('[]')  # would be called if memories existed
        result = generate_examples(h)
        assert result == BUILTIN_EXAMPLES
        assert h.router.calls == 0  # no LLM without memories

    def test_memory_personalizes_and_caches(self) -> None:
        h = Harness(memory_db=":memory:")
        h.remember("theme", "暗色主题")
        h.router = Router(
            '[{"label": "暗色页面", "text": "写一个暗色主题的 HTML 页面保存为 dark.html"},'
            '{"label": "主题文档", "text": "生成一份暗色主题的样式说明"}]'
        )
        result = generate_examples(h)
        assert h.router.calls == 1
        assert len(result) == len(BUILTIN_EXAMPLES) + 2
        # personalized ones come AFTER the built-ins
        assert result[-2]["text"] == "写一个暗色主题的 HTML 页面保存为 dark.html"
        assert result[-1]["text"] == "生成一份暗色主题的样式说明"

        # cached: second call does not hit the model again
        again = generate_examples(h)
        assert h.router.calls == 1
        assert again == result

    def test_force_regenerates(self) -> None:
        h = Harness(memory_db=":memory:")
        h.remember("theme", "暗色主题")
        h.router = Router('[{"label": "暗色页面", "text": "创建暗色主题 HTML 页面保存为 dark.html"}]')
        first = generate_examples(h)
        h.router.payload = '[{"label": "配色报告", "text": "计算五组颜色在深色背景上的对比度并输出报告"}]'
        second = generate_examples(h, force=True)
        assert h.router.calls == 2
        assert second[-1]["label"] == "配色报告"

    def test_bad_model_output_falls_back_to_builtin(self) -> None:
        h = Harness(memory_db=":memory:")
        h.remember("x", "y")
        h.router = Router("这完全不是 JSON")
        result = generate_examples(h)
        assert result == BUILTIN_EXAMPLES  # graceful fallback

    def test_rejects_root_paths_keeps_good_items(self) -> None:
        """Unresolvable absolute paths (e.g. /app/config/editor.json) are dropped;
        resolvable items in the same batch survive."""
        h = Harness(memory_db=":memory:")
        h.remember("theme", "暗色主题")
        h.router = Router(
            '[{"label": "改配置", "text": "将 /app/config/editor.json 中所有 theme 字段改为 dark"},'
            '{"label": "暗色页面", "text": "创建暗色主题的 HTML 页面 dark.html，包含标题、说明文字和一个表格，保存到沙箱"}]'
        )
        result = generate_examples(h)
        assert h.router.calls == 1  # at least one good item -> no retry needed
        texts = [e["text"] for e in result]
        assert "dark.html" in texts[-1]
        assert not any("editor.json" in t for t in texts)
        assert len(result) == len(BUILTIN_EXAMPLES) + 1

    def test_all_bad_retries_then_falls_back(self) -> None:
        h = Harness(memory_db=":memory:")
        h.remember("x", "y")
        h.router = Router(
            '[{"label": "改配置", "text": "将 /app/config/editor.json 的 theme 改为 dark"},'
            '{"label": "Temp", "text": "读取 /tmp/data.csv 并统计"}]'
        )
        result = generate_examples(h)
        assert result == BUILTIN_EXAMPLES
        assert h.router.calls == 2  # one retry before giving up
