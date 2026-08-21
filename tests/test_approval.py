"""Tests for the human-in-the-loop approval gate.

Drives the compiled graph with a scripted FakeRouter and a real MemorySaver
checkpointer - no LLM or network involved. The flow under test:

    agent -> human_gate -> interrupt() -> [human decides]
          -> Command(resume=decisions) -> tools -> agent -> END
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

from agentpulse.graph.loop import build_loop
from agentpulse.harness import Harness
from agentpulse.tools.registry import ToolRegistry
from agentpulse.cli import _resolve_approvals

import agentpulse.codegen as _codegen  # patched inside make_harness to keep the loop the only driver
from agentpulse.api import create_app
from fastapi.testclient import TestClient

from test_loop import FakeRouter, SYSTEM, answer, tool_call

CONFIG = {"configurable": {"thread_id": "test-thread"}}


@pytest.fixture(autouse=True)
def _restore_codegen_solve():
    """make_harness stubs agentpulse.codegen.codegen_solve for the duration of
    a test; this fixture guarantees the stub never leaks into other test
    modules (test_codegen.py runs after this one alphabetically)."""
    original = _codegen.codegen_solve
    yield
    _codegen.codegen_solve = original


def make_registry(*, flagged: str | None = None) -> ToolRegistry:
    reg = ToolRegistry()

    @reg.register
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    if flagged:

        @reg.register(require_approval=True)
        def risky(path: str, content: str = "x") -> str:
            """Write a sandbox file (side effect)."""
            return f"wrote {path}"

    return reg


def build(router: FakeRouter, registry: ToolRegistry):
    return build_loop(
        router,
        registry,
        system_prompt=SYSTEM,
        needs_approval=registry.needs_approval,
    )


def invoke(loop, state):
    return loop.invoke(state, CONFIG)


class TestApprovalGate:
    def test_unflagged_tool_bypasses_gate(self) -> None:
        """No require_approval tools -> same behavior as before, no interrupt."""
        router = FakeRouter([tool_call("add", {"a": 2, "b": 3}), answer("the sum is 5")])
        reg = make_registry()  # add only, nothing flagged
        loop = build(router, reg)
        final = invoke(loop, {"messages": [HumanMessage(content="hi")], "step": 0, "max_steps": 5})
        assert final["step"] == 2
        assert "__interrupt__" not in final
        assert any(isinstance(m, ToolMessage) and m.content == "5" for m in final["messages"])

    def test_flagged_tool_interrupts_with_payload(self) -> None:
        """A flagged tool call stops at the gate; __interrupt__ lists it."""
        router = FakeRouter([tool_call("risky", {"path": "a.txt"})])
        reg = make_registry(flagged=True)
        loop = build(router, reg)
        final = invoke(loop, {"messages": [HumanMessage(content="write a.txt")], "step": 0, "max_steps": 5})
        assert "__interrupt__" in final
        interrupts = final["__interrupt__"]
        payload = interrupts[0].value
        assert payload["tool_calls"] == [
            {"id": "call_risky", "name": "risky", "args": {"path": "a.txt"}}
        ]

    def test_resume_approve_executes_tool(self) -> None:
        router = FakeRouter([tool_call("risky", {"path": "a.txt"}), answer("done writing")])
        reg = make_registry(flagged=True)
        loop = build(router, reg)
        state = {"messages": [HumanMessage(content="write a.txt")], "step": 0, "max_steps": 5}
        final = invoke(loop, state)
        assert "__interrupt__" in final

        resumed = loop.invoke(
            Command(resume=[{"id": "call_risky", "action": "approve"}]), CONFIG
        )
        assert "__interrupt__" not in resumed
        results = [m.content for m in resumed["messages"] if isinstance(m, ToolMessage)]
        assert results == ["wrote a.txt"]
        texts = [m.content for m in resumed["messages"] if getattr(m, "type", "") == "ai"]
        assert texts[-1] == "done writing"

    def test_resume_deny_injects_denial_message(self) -> None:
        """Denied call never executes; the model sees a denial ToolMessage and
        must still receive a paired tool_call_id response."""
        router = FakeRouter([tool_call("risky", {"path": "a.txt"}), answer("okay, skipped")])
        reg = make_registry(flagged=True)
        loop = build(router, reg)
        state = {"messages": [HumanMessage(content="write a.txt")], "step": 0, "max_steps": 5}
        invoke(loop, state)

        resumed = loop.invoke(
            Command(
                resume=[
                    {"id": "call_risky", "action": "deny", "reason": "wrong file"}
                ]
            ),
            CONFIG,
        )
        results = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
        assert len(results) == 1
        assert "denied" in results[0].content
        assert "wrong file" in results[0].content
        # tool_call_id pairing survived the denial
        assert results[0].tool_call_id == "call_risky"
        # the second LLM call carries the paired tool message
        tool_msgs = [m for m in router.seen[1] if m.get("role") == "tool"]
        assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_risky"

    def test_resume_edit_replaces_args(self) -> None:
        router = FakeRouter([tool_call("risky", {"path": "a.txt"}), answer("done")])
        reg = make_registry(flagged=True)
        loop = build(router, reg)
        state = {"messages": [HumanMessage(content="write a.txt")], "step": 0, "max_steps": 5}
        invoke(loop, state)

        resumed = loop.invoke(
            Command(
                resume=[
                    {"id": "call_risky", "action": "edit", "args": {"path": "b.txt"}}
                ]
            ),
            CONFIG,
        )
        results = [m.content for m in resumed["messages"] if isinstance(m, ToolMessage)]
        assert results == ["wrote b.txt"]

    def test_resume_string_applies_to_all_pending(self) -> None:
        """Convenience: a bare 'approve'/'deny' string decides every pending call."""
        router = FakeRouter([tool_call("risky", {"path": "a.txt"}), answer("done")])
        reg = make_registry(flagged=True)
        loop = build(router, reg)
        state = {"messages": [HumanMessage(content="write a.txt")], "step": 0, "max_steps": 5}
        invoke(loop, state)
        resumed = loop.invoke(Command(resume="approve"), CONFIG)
        results = [m.content for m in resumed["messages"] if isinstance(m, ToolMessage)]
        assert results == ["wrote a.txt"]

    def test_uncovered_call_defaults_to_deny(self) -> None:
        """Decisions that skip a pending call id are treated as denied."""
        router = FakeRouter([tool_call("risky", {"path": "a.txt"}), answer("ok")])
        reg = make_registry(flagged=True)
        loop = build(router, reg)
        state = {"messages": [HumanMessage(content="write a.txt")], "step": 0, "max_steps": 5}
        invoke(loop, state)
        resumed = loop.invoke(Command(resume=[]), CONFIG)
        results = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
        assert len(results) == 1
        assert "denied" in results[0].content

    def test_chained_interrupts(self) -> None:
        """After resuming, a NEW flagged call in a later step interrupts again."""
        router = FakeRouter(
            [
                tool_call("risky", {"path": "a.txt"}),
                tool_call("risky", {"path": "b.txt"}),
                answer("both written"),
            ]
        )
        reg = make_registry(flagged=True)
        loop = build(router, reg)
        state = {"messages": [HumanMessage(content="write two files")], "step": 0, "max_steps": 5}
        r1 = invoke(loop, state)
        assert r1["__interrupt__"][0].value["tool_calls"][0]["args"] == {"path": "a.txt"}

        r2 = loop.invoke(Command(resume="approve"), CONFIG)
        assert r2["__interrupt__"][0].value["tool_calls"][0]["args"] == {"path": "b.txt"}

        r3 = loop.invoke(Command(resume="approve"), CONFIG)
        assert "__interrupt__" not in r3
        results = [m.content for m in r3["messages"] if isinstance(m, ToolMessage)]
        assert results == ["wrote a.txt", "wrote b.txt"]

    def test_mixed_batch_flagged_and_safe(self) -> None:
        """A batch mixing flagged and unflagged calls: gate interrupts only for
        the flagged one; the safe one still executes after resume."""
        batch = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add", "arguments": json.dumps({"a": 1, "b": 2})},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "risky", "arguments": json.dumps({"path": "a.txt"})},
                },
            ],
        }
        router = FakeRouter([batch, answer("done")])
        reg = make_registry(flagged=True)
        loop = build(router, reg)
        state = {"messages": [HumanMessage(content="go")], "step": 0, "max_steps": 5}
        r1 = invoke(loop, state)
        # only the flagged call is up for approval
        assert [c["name"] for c in r1["__interrupt__"][0].value["tool_calls"]] == ["risky"]

        resumed = loop.invoke(
            Command(resume=[{"id": "call_2", "action": "approve"}]), CONFIG
        )
        results = [m.content for m in resumed["messages"] if isinstance(m, ToolMessage)]
        assert results == ["3", "wrote a.txt"]

    def test_approval_events_emitted(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        router = FakeRouter([tool_call("risky", {"path": "a.txt"}), answer("done")])
        reg = make_registry(flagged=True)
        loop = build_loop(
            router,
            reg,
            system_prompt=SYSTEM,
            needs_approval=reg.needs_approval,
            emit=lambda kind, data: events.append((kind, data)),
        )
        state = {"messages": [HumanMessage(content="go")], "step": 0, "max_steps": 5}
        r1 = invoke(loop, state)
        assert "__interrupt__" in r1
        kinds = [k for k, _ in events]
        assert "approval_request" in kinds  # gate announced the pending calls
        resumed = loop.invoke(Command(resume="approve"), CONFIG)
        assert "__interrupt__" not in resumed
        kinds = [k for k, _ in events]
        assert "approval_result" in kinds


def make_harness(router_script: list[dict[str, Any]], tmp_path) -> Harness:
    """A Harness with no builtin tools and one approval-gated tool. The fake
    router is swapped in BEFORE the gated tool is registered, so the loop the
    gate-enabling rebuild captures is the fake one."""
    h = Harness(
        memory_db=":memory:",
        register_builtin_tools=False,
        sandbox_dir=None,
    )
    # codegen_solve normally runs before the loop; here it would steal the
    # scripted router response meant for the loop's approval gate. Disable it
    # so the harness drives the loop directly (this test only exercises the gate).
    _codegen.codegen_solve = lambda *a, **k: None  # type: ignore[assignment]
    h.router = FakeRouter(router_script)

    @h.register_tool(require_approval=True)
    def risky(path: str) -> str:
        """Write a sandbox file (side effect)."""
        return f"wrote {path}"

    return h


class TestHarnessApproval:
    def test_run_suspends_and_resolve_finishes(self, tmp_path) -> None:
        h = make_harness(
            [tool_call("risky", {"path": "a.txt"}), answer("file written")], tmp_path
        )
        try:
            reply = h.run("write a.txt")
            assert reply == ""
            assert h.pending_approval is not None
            assert h.pending_approval["tool_calls"][0]["name"] == "risky"
            # user message is in the transcript, assistant reply not yet
            assert [m["role"] for m in h.transcript] == ["user"]

            reply = h.resolve_approval([{"id": "call_risky", "action": "approve"}])
            assert reply == "file written"
            assert h.pending_approval is None
            assert [m["role"] for m in h.transcript] == ["user", "assistant"]
            assert h.transcript[-1]["content"] == "file written"
        finally:
            h.close()

    def test_resolve_without_pending_raises(self, tmp_path) -> None:
        h = make_harness([answer("hi")], tmp_path)
        try:
            with pytest.raises(RuntimeError, match="no approval is pending"):
                h.resolve_approval("approve")
        finally:
            h.close()

    def test_new_run_auto_denies_dangling_approval(self, tmp_path) -> None:
        """The caller ignored the approval and sent a new message: the pending
        turn is closed with a deny, then the new turn runs normally."""
        h = make_harness(
            [
                tool_call("risky", {"path": "a.txt"}),  # turn 1: suspends
                answer("denied, moving on"),            # turn 1 resumes (deny)
                answer("second turn reply"),            # turn 2
            ],
            tmp_path,
        )
        try:
            h.run("write a.txt")
            assert h.pending_approval is not None
            reply = h.run("never mind, just say hi")
            assert reply == "second turn reply"
            assert h.pending_approval is None
            roles = [m["role"] for m in h.transcript]
            assert roles == ["user", "assistant", "user", "assistant"]
        finally:
            h.close()

    def test_chained_approval_within_one_turn(self, tmp_path) -> None:
        """Resuming may suspend again on a later flagged call."""
        h = make_harness(
            [
                tool_call("risky", {"path": "a.txt"}),
                tool_call("risky", {"path": "b.txt"}),
                answer("both written"),
            ],
            tmp_path,
        )
        try:
            assert h.run("write two files") == ""
            assert h.pending_approval["tool_calls"][0]["args"] == {"path": "a.txt"}
            assert h.resolve_approval("approve") == ""
            assert h.pending_approval["tool_calls"][0]["args"] == {"path": "b.txt"}
            assert h.resolve_approval("approve") == "both written"
            assert h.pending_approval is None
        finally:
            h.close()


class TestApprovalApi:
    """API surface: chat suspends with pending_approval, approve resumes it."""

    def test_chat_suspends_then_approve_finishes(self, tmp_path) -> None:
        h = make_harness(
            [tool_call("risky", {"path": "a.txt"}), answer("file written")], tmp_path
        )
        client = TestClient(create_app(harness=h))
        try:
            res = client.post("/api/chat", json={"message": "write a.txt"})
            assert res.status_code == 200
            body = res.json()
            assert body["status"] == "pending_approval"
            assert body["reply"] == ""
            assert body["pending"][0]["name"] == "risky"
            assert body["pending"][0]["args"] == {"path": "a.txt"}
            tid = body["thread_id"]
            assert tid

            res2 = client.post(
                "/api/chat/approve", json={"thread_id": tid, "decisions": "approve"}
            )
            assert res2.status_code == 200
            body2 = res2.json()
            assert body2["status"] == "done"
            assert body2["reply"] == "file written"
            assert [m["role"] for m in body2["transcript"]] == ["user", "assistant"]
        finally:
            h.close()

    def test_approve_wrong_thread_rejected(self, tmp_path) -> None:
        h = make_harness(
            [tool_call("risky", {"path": "a.txt"}), answer("x")], tmp_path
        )
        client = TestClient(create_app(harness=h))
        try:
            client.post("/api/chat", json={"message": "go"})
            res = client.post(
                "/api/chat/approve",
                json={"thread_id": "no-such-thread", "decisions": "approve"},
            )
            assert res.status_code == 409
        finally:
            h.close()


class TestCliApproval:
    """Terminal approval loop (driven with a patched builtins.input)."""

    def test_resolve_via_approve_all(self, tmp_path, monkeypatch) -> None:
        h = make_harness(
            [tool_call("risky", {"path": "a.txt"}), answer("file written")], tmp_path
        )
        replies = iter(["y"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))
        try:
            assert h.run("write a.txt") == ""  # suspends
            reply = _resolve_approvals(h, "")
            assert reply == "file written"
            assert h.pending_approval is None
        finally:
            h.close()

    def test_resolve_via_deny_all(self, tmp_path, monkeypatch) -> None:
        h = make_harness(
            [
                tool_call("risky", {"path": "a.txt"}),
                answer("okay, skipped the write"),
            ],
            tmp_path,
        )
        replies = iter(["n"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))
        try:
            assert h.run("write a.txt") == ""
            reply = _resolve_approvals(h, "")
            assert reply == "okay, skipped the write"
            assert h.pending_approval is None
        finally:
            h.close()

    def test_resolve_via_edit_args(self, tmp_path, monkeypatch) -> None:
        h = make_harness(
            [tool_call("risky", {"path": "a.txt"}), answer("done")], tmp_path
        )
        # top-level [e], then per-call sub-choice [e], then new-args JSON
        replies = iter(["e", "e", '{"path": "b.txt"}'])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))
        try:
            assert h.run("write a.txt") == ""
            reply = _resolve_approvals(h, "")
            assert reply == "done"
            # the edited args reached the tool (b.txt not a.txt)
            assert any("wrote b.txt" in (r.get("content") or "") for r in h.last_trace)
        finally:
            h.close()


class TestMemoryHint:
    """Mechanism-level memory pre-fetch: matching snapshots are injected into
    the chat context as a system message before the model picks a tool."""

    def _hint_harness(self, tmp_path) -> Harness:
        h = Harness(
            memory_db=str(tmp_path / "mem.db"),
            register_builtin_tools=False,
            sandbox_dir=None,
        )
        h.long_term.remember(
            "美元/人民币汇率报告",
            "[2026-08-20 19:32] 当前汇率: 6.7237 涨跌额: -0.0043 涨跌幅: -0.06%",
            scope="default",
        )
        return h

    def test_related_question_injects_snapshot(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("agentpulse.codegen.codegen_solve", lambda *a, **k: None)
        h = self._hint_harness(tmp_path)
        router = FakeRouter([answer("根据记忆快照回答。")])
        h.router = router
        h._loop = build_loop(
            router, h.chat_tools,
            system_prompt=h.settings.system_prompt,
            verbose=h.settings.verbose,
            needs_approval=h._needs_approval,
        )
        h.run("查一下美元兑人民币的汇率")
        msgs = router.seen[0]
        assert any(
            m.get("role") == "system" and "美元/人民币汇率报告" in str(m.get("content", ""))
            for m in msgs
        )
        h.close()

    def test_unrelated_question_no_injection(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("agentpulse.codegen.codegen_solve", lambda *a, **k: None)
        h = self._hint_harness(tmp_path)
        router = FakeRouter([answer("多云 25 度。")])
        h.router = router
        h._loop = build_loop(
            router, h.chat_tools,
            system_prompt=h.settings.system_prompt,
            verbose=h.settings.verbose,
            needs_approval=h._needs_approval,
        )
        h.run("今天深圳天气怎么样")
        msgs = router.seen[0]
        assert not any(
            m.get("role") == "system" and "Stored memories" in str(m.get("content", ""))
            for m in msgs
        )
        h.close()
