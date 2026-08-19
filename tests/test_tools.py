"""Tests for the tool registry and built-in tools."""

from __future__ import annotations

import pytest

from agentpulse.memory import LongTermMemory
from agentpulse.tools.builtin import register_builtins
from agentpulse.tools.registry import ToolError, ToolRegistry


class TestRegistry:
    def test_register_and_execute(self) -> None:
        reg = ToolRegistry()

        @reg.register
        def echo(text: str) -> str:
            """Echo back the text."""
            return text

        assert reg.get("echo") is not None
        assert reg.execute("echo", {"text": "hi"}) == "hi"
        assert reg.names() == ["echo"]

    def test_register_with_explicit_metadata(self) -> None:
        reg = ToolRegistry()

        @reg.register(name="multiply", description="Multiply two numbers")
        def mul(a: int, b: int) -> int:
            return a * b

        schema = reg.get("multiply").schema()
        assert schema["function"]["name"] == "multiply"
        assert schema["function"]["description"] == "Multiply two numbers"
        assert schema["function"]["parameters"]["required"] == ["a", "b"]
        assert reg.execute("multiply", {"a": 3, "b": 4}) == "12"

    def test_duplicate_name_raises(self) -> None:
        reg = ToolRegistry()
        reg.register(lambda: 1, name="dup")
        with pytest.raises(ValueError, match="already registered"):
            reg.register(lambda: 2, name="dup")

    def test_unknown_tool_raises(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(ToolError, match="unknown tool"):
            reg.execute("missing", {})

    def test_result_serialization(self) -> None:
        reg = ToolRegistry()
        reg.register(lambda: {"a": 1}, name="obj")
        assert reg.execute("obj") == '{"a": 1}'


class TestBuiltins:
    @pytest.fixture()
    def registry(self, tmp_path) -> ToolRegistry:
        memory = LongTermMemory(tmp_path / "m.db")
        reg = ToolRegistry()
        register_builtins(reg, memory)
        yield reg
        memory.close()

    def test_builtin_names(self, registry: ToolRegistry) -> None:
        assert set(registry.names()) == {
            "get_current_time",
            "add",
            "remember",
            "recall",
            "list_memories",
        }

    def test_add(self, registry: ToolRegistry) -> None:
        assert registry.execute("add", {"a": 2.5, "b": 1.5}) == "4.0"

    def test_get_current_time(self, registry: ToolRegistry) -> None:
        result = registry.execute("get_current_time", {})
        import datetime

        datetime.datetime.fromisoformat(result)  # must parse
        assert "+" in result  # has tz offset

    def test_remember_then_recall(self, registry: ToolRegistry) -> None:
        registry.execute("remember", {"key": "theme", "value": "dark"})
        assert registry.execute("recall", {"key": "theme"}) == "theme = dark"
        assert registry.execute("recall", {"key": "missing"}) == "在 scope 'default' 中未找到 'missing'"

    def test_list_memories(self, registry: ToolRegistry) -> None:
        registry.execute("remember", {"key": "a", "value": 1})
        registry.execute("remember", {"key": "b", "value": 2})
        listing = registry.execute("list_memories", {})
        assert "- a" in listing and "- b" in listing
