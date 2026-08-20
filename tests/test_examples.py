"""Tests for the example-task service (fixed built-in set)."""

from __future__ import annotations

from agentpulse.examples import BUILTIN_EXAMPLES, generate_examples


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
