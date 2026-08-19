"""Tests for the LangGraph loop: routing, tool execution, and the max_steps bound.

These tests drive the compiled graph directly with a scripted FakeRouter,
so no real LLM or network is involved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentpulse.graph.loop import build_loop
from agentpulse.tools.registry import ToolRegistry

SYSTEM = "test system prompt"


class FakeRouter:
    """Scripted stand-in for LiteLLMRouter: returns canned responses in order."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.seen: list[list[dict[str, Any]]] = []
        self.tools_seen: list[Any] = []

    def complete(self, messages, tools=None, **kwargs):
        self.seen.append(messages)
        self.tools_seen.append(tools)
        if not self.script:
            return {"role": "assistant", "content": "(ran out of script)"}
        return self.script.pop(0)

    def parse_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for tc in message.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn["arguments"])
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc["id"], "name": fn["name"], "arguments": args})
        return calls


def answer(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.register
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    return reg


def run_loop(router: FakeRouter, *, max_steps: int, registry: ToolRegistry | None = None):
    loop = build_loop(router, registry or make_registry(), system_prompt=SYSTEM)
    return loop.invoke(
        {
            "messages": [HumanMessage(content="hello")],
            "step": 0,
            "max_steps": max_steps,
        }
    )


def roles_and_texts(messages) -> list[tuple[str, str]]:
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append((m.get("role", ""), str(m.get("content", ""))))
        elif isinstance(m, AIMessage):
            calls = ",".join(tc["name"] for tc in m.tool_calls) if m.tool_calls else ""
            out.append(("assistant", (m.content or "") + (f" [calls:{calls}]" if calls else "")))
        elif isinstance(m, ToolMessage):
            out.append(("tool", str(m.content)))
        else:
            out.append((getattr(m, "type", "?"), str(m.content)))
    return out


class TestLoop:
    def test_single_answer_no_tools(self) -> None:
        """No tool calls -> one agent step, loop ends."""
        router = FakeRouter([answer("hi there")])
        final = run_loop(router, max_steps=5)
        assert final["step"] == 1
        assert roles_and_texts(final["messages"])[-1] == ("assistant", "hi there")
        # system prompt was injected, no tools sent (registry empty path is covered below)
        assert router.seen[0][0]["role"] == "system"

    def test_tool_then_final_answer(self) -> None:
        """agent -> tool -> agent(final answer) => loop ends with tool result visible."""
        router = FakeRouter([tool_call("add", {"a": 2, "b": 3}), answer("the sum is 5")])
        final = run_loop(router, max_steps=5)
        roles = [r for r, _ in roles_and_texts(final["messages"])]
        assert roles == ["human", "assistant", "tool", "assistant"]
        assert any(r == "tool" and t == "5" for r, t in roles_and_texts(final["messages"]))
        assert roles_and_texts(final["messages"])[-1] == ("assistant", "the sum is 5")
        assert final["step"] == 2

    def test_tool_error_returns_to_model(self) -> None:
        """Unknown tool: error string becomes a ToolMessage, loop still proceeds."""
        router = FakeRouter([tool_call("does_not_exist", {}), answer("ok")])
        registry = make_registry()
        final = run_loop(router, max_steps=5, registry=registry)
        roles_and_text = roles_and_texts(final["messages"])
        assert any(r == "tool" and "unknown tool" in t for r, t in roles_and_text)

    def test_max_steps_halts_runaway_loop(self) -> None:
        """Model keeps calling tools: the loop must stop at max_steps."""
        always_tool = [tool_call("add", {"a": 1, "b": 1}) for _ in range(10)]
        router = FakeRouter(always_tool)
        final = run_loop(router, max_steps=2)
        # step increments on every agent call; with max_steps=2 the loop ends
        # after the 2nd agent call (1 tool round executed).
        assert final["step"] == 2
        tool_rounds = sum(1 for r, _ in roles_and_texts(final["messages"]) if r == "tool")
        assert tool_rounds == 1

    def test_system_prompt_and_tools_passed_to_model(self) -> None:
        router = FakeRouter([answer("done")])
        registry = make_registry()
        run_loop(router, max_steps=3, registry=registry)
        sent = router.seen[0]
        assert sent[0] == {"role": "system", "content": SYSTEM}
        assert sent[1]["role"] == "user"
        # tools schema should be attached to the completion call
        tools = router.tools_seen[0]
        assert tools is not None
        names = {t["function"]["name"] for t in tools}
        assert "add" in names

    def test_batched_tool_calls_all_executed(self) -> None:
        """One assistant message may carry multiple tool_calls; ALL must run
        (this is what keeps multi-step computation to a single round-trip)."""
        multi = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add", "arguments": json.dumps({"a": 23, "b": 45})},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "add", "arguments": json.dumps({"a": 67, "b": 89})},
                },
            ],
        }
        router = FakeRouter([multi, answer("done")])
        final = run_loop(router, max_steps=5)
        results = [m.content for m in final["messages"] if isinstance(m, ToolMessage)]
        assert results == ["68", "156"]

    def test_tool_messages_carry_tool_call_id(self) -> None:
        """Regression: OpenAI-compatible APIs reject role=tool messages without
        tool_call_id (agnes gateway: json_parse_error). The 2nd LLM call must
        pair each tool result with its originating tool_call id."""
        router = FakeRouter([tool_call("add", {"a": 1, "b": 2}), answer("3")])
        run_loop(router, max_steps=5)
        assert len(router.seen) >= 2, "loop should call the model twice"
        second_call = router.seen[1]

        tool_msgs = [m for m in second_call if m.get("role") == "tool"]
        assert tool_msgs, "expected a tool message in the 2nd LLM call"
        assert all(m.get("tool_call_id") for m in tool_msgs), (
            f"tool messages must carry tool_call_id, got: {tool_msgs}"
        )

        assistant_call_ids = [
            tc["id"]
            for m in second_call
            if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        ]
        assert assistant_call_ids, "expected assistant tool_calls in the 2nd call"
        assert tool_msgs[0]["tool_call_id"] == assistant_call_ids[0]
