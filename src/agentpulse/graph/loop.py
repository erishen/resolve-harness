"""The Loop: a LangGraph StateGraph wiring agent reasoning, tool execution,
and the routing decision that keeps the loop bounded.

    START ──> agent ──(has tool_calls & step<max)──> tools ──> agent
                 │
                 └──(no tool_calls or step>=max)──> END

With a human-in-the-loop gate (pass `needs_approval`), flagged tool calls
detour through `human_gate` before execution:

    agent ──(flagged tool_calls)──> human_gate ──> tools ──> agent
                                       │
                                       └─ interrupt() suspends the graph;
                                          resume with Command(resume=[...])
                                          re-enters the gate, which records
                                          the human's decisions in state and
                                          lets `tools` act on them.

Nodes are built as closures over the harness's router and tool registry so
the graph itself stays stateless and testable.

Messages inside the state are LangChain message objects (the `add_messages`
reducer normalizes them); the node boundaries below convert to/from the
OpenAI-shaped dicts that LiteLLM expects.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..llm import LiteLLMRouter
from ..tools.registry import ToolRegistry
from .state import AgentState

logger = logging.getLogger(__name__)

EDGE_TOOLS = "tools"
EDGE_GATE = "human_gate"
EDGE_END = "end"

# Max message count sent to the LLM per agent step. Multi-turn chats grow the
# history (every tool_call/tool_result pair is re-sent in full), so we trim the
# oldest turns beyond this — long-term facts stay available via recall.
MAX_LLM_MESSAGES = 24


def _trim_history(history: list[Any], max_msgs: int = MAX_LLM_MESSAGES) -> list[Any]:
    """Keep the newest `max_msgs` messages without breaking tool-call pairing.

    OpenAI-compatible APIs require every ToolMessage to be preceded by the
    assistant message carrying its tool_calls. When the cut point lands on a
    ToolMessage we extend backwards to include that paired assistant turn, so
    the trimmed history always starts on a complete round.
    """
    if len(history) <= max_msgs:
        return history
    keep = history[-max_msgs:]
    while keep and isinstance(keep[0], ToolMessage):
        idx = len(history) - len(keep)
        if idx <= 0:
            break
        keep = history[idx - 1 :]
    return keep


def build_loop(
    router: LiteLLMRouter,
    registry: ToolRegistry,
    *,
    system_prompt: str,
    verbose: bool = False,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    needs_approval: Callable[[str], bool] | None = None,
    model: str | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Build and compile the agent loop graph.

    Args:
        router: model router used by the agent node.
        registry: tools available to the agent.
        system_prompt: system message prepended to every completion.
        verbose: log each step to stderr.
        emit: optional callback fired on process events, so a task runner /
            UI can stream the loop's inner workings live:
            - ("node_enter", {"node", "step"})   — a graph node began running
                                                (agent / tools / human_gate)
            - ("route", {"to", "reason", "step", "max_steps"}) — the conditional
                                                edge decision after an agent step
            - ("thought", {"content": ...})       — agent reasoning text
            - ("tool_call", {"name", "args"})     — agent decided to call a tool
            - ("tool_result", {"name", "content"})— tool execution result
            - ("approval_request", {"tool_calls"})— gate has calls awaiting a human
            - ("approval_result", {"decisions"})  — human decisions were applied
            Called from worker threads; must be thread-safe.
        needs_approval: predicate naming tools whose calls must pass a human
            approval gate before execution. When given, the graph compiles
            with an in-memory checkpointer and `invoke` must carry
            `config={"configurable": {"thread_id": ...}}`; a flagged call
            surfaces as `result["__interrupt__"]`, and the caller resumes with
            `graph.invoke(Command(resume=[...]), config)`.

            A resume value is either a bare "approve"/"deny" string (applied
            to every pending call) or a list of decisions
            `{"id", "action": "approve"|"deny"|"edit", "reason"?, "args"?}`;
            pending calls left without a decision default to deny.

    Returns a callable that accepts an initial `AgentState` dict and returns
    the final state (with `messages` containing the full transcript).
    """

    # -- nodes ---------------------------------------------------------------

    def agent_node(state: AgentState) -> dict[str, Any]:
        step = state.get("step", 0) + 1
        history = state.get("messages", []) or []
        if emit:
            emit("node_enter", {"node": "agent", "step": step})
        llm_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        llm_messages.extend(_to_llm_messages(_trim_history(history)))

        response = router.complete(
            llm_messages,
            tools=registry.schemas() if len(registry) else None,
            # 会话级模型覆盖优先，缺省回退 loop 构造时固定的 model。
            model=state.get("model") or model,
        )
        tool_calls = router.parse_tool_calls(response)
        content = response.get("content") or ""
        if verbose:
            calls = ", ".join(tc["name"] for tc in tool_calls) or "none"
            logger.info("[agent] step=%s tool_calls=%s", step, calls)
        if emit:
            if content:
                emit("thought", {"content": content, "step": step})
            for tc in tool_calls:
                emit("tool_call", {"name": tc["name"], "args": tc["arguments"], "step": step})

        return {
            "messages": [
                AIMessage(
                    content=content,
                    tool_calls=[
                        {
                            "name": tc["name"],
                            "args": tc["arguments"],
                            "id": tc["id"] or f"call_{step}_{i}",
                            "type": "tool_call",
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                )
            ],
            "step": step,
        }

    def tools_node(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        if emit:
            emit("node_enter", {"node": "tools", "step": state.get("step", 0)})
        # Decisions from the gate: call_id -> {"action", "reason"?, "args"?}
        approvals = state.get("approvals") or {}
        tool_messages: list[ToolMessage] = []
        for tc in last.tool_calls:
            name = tc["name"]
            args = tc.get("args") or {}
            call_id = tc.get("id") or ""
            decision = approvals.get(call_id)
            if decision is not None and decision.get("action") == "deny":
                # Refused by the human: never execute; tell the model why so
                # it can adjust (the ToolMessage keeps the pairing intact).
                result = f"User denied this tool call: {decision.get('reason') or 'no reason given'}"
            else:
                if decision is not None and decision.get("action") == "edit":
                    # Edited args replace the original ones wholesale.
                    args = decision.get("args") or args
                if verbose:
                    logger.info("[tools] %s(%s)", name, args)
                try:
                    result = registry.execute(name, args)
                except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
                    result = f"Tool error: {exc}"
            if emit:
                emit("tool_result", {"name": name, "content": result, "step": state.get("step", 0)})
                # run_script 通过子进程写文件，不会走 write_file 工具，故单独广播
                # 产出文件事件，让任务完成界面能列出真实落盘的制品（如 futures.md）。
                if name == "run_script" and "退出码 0" in result:
                    out_path = _run_script_out_path(args)
                    if out_path:
                        emit("produced_file", {"path": out_path, "name": out_path})
            tool_messages.append(ToolMessage(content=result, tool_call_id=call_id))
        # Decisions are consumed; reset so a stale id can never leak into a
        # later round (call ids are unique per step, but cheap to be sure).
        return {"messages": tool_messages, "approvals": {}}

    # -- human-in-the-loop gate ------------------------------------------------

    # interrupt() raises on the first pass and RETURNS the resume value when
    # the node re-runs after Command(resume=...) - so the emit below would
    # fire twice per approval round. Remember announced batches to dedupe.
    announced_batches: set[tuple[Any, ...]] = set()

    def human_gate(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        if emit:
            emit("node_enter", {"node": "human_gate", "step": state.get("step", 0)})
        pending = [
            {"id": tc.get("id") or "", "name": tc["name"], "args": tc.get("args") or {}}
            for tc in last.tool_calls
            if needs_approval and needs_approval(tc["name"])
        ]
        if not pending:  # routing guarantees this doesn't happen; stay safe
            return {"approvals": {}}

        batch_key = (state.get("step", 0), tuple(c["id"] for c in pending))
        if batch_key not in announced_batches:
            announced_batches.add(batch_key)
            if emit:
                emit("approval_request", {"tool_calls": pending, "step": state.get("step", 0)})

        # Suspend here. First run: raises GraphInterrupt, invoke() returns the
        # payload below as result["__interrupt__"]. Resume run: returns the
        # human's decision(s) instead.
        resume_value = interrupt({"tool_calls": pending})
        decisions = _normalize_decisions(resume_value, pending)
        if emit:
            emit("approval_result", {"decisions": decisions, "step": state.get("step", 0)})

        approvals: dict[str, dict[str, Any]] = {}
        for call in pending:
            decision = decisions.get(call["id"])
            if decision is None:
                # No decision for this call: conservative deny. The model sees
                # the refusal and can ask differently or move on.
                decision = {"id": call["id"], "action": "deny", "reason": "no decision given"}
            approvals[call["id"]] = decision
        return {"approvals": approvals}

    # -- routing ---------------------------------------------------------------

    def _flagged_calls(state: AgentState) -> list[Any]:
        last = state["messages"][-1]
        if needs_approval is None:
            return []
        return [tc for tc in (getattr(last, "tool_calls", None) or []) if needs_approval(tc["name"])]

    def should_continue(state: AgentState) -> str:
        step = state.get("step", 0)
        max_steps = state.get("max_steps", 1)
        last = state["messages"][-1]
        has_calls = bool(getattr(last, "tool_calls", None))
        if has_calls and step < max_steps:
            if _flagged_calls(state):
                target, reason = EDGE_GATE, "approval_needed"
            else:
                target, reason = EDGE_TOOLS, "tool_calls"
        elif has_calls:
            logger.warning(
                "[loop] hit max_steps=%s with pending tool calls; ending loop", max_steps
            )
            target, reason = EDGE_END, "max_steps"
        else:
            target, reason = EDGE_END, "no_tool_calls"
        if emit:
            emit(
                "route",
                {
                    "to": target,
                    "reason": reason,
                    "step": step,
                    "max_steps": max_steps,
                    "has_tool_calls": has_calls,
                },
            )
        return target

    # -- graph assembly ----------------------------------------------------------

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    route_map: dict[str, Any] = {EDGE_TOOLS: "tools", EDGE_END: END}
    if needs_approval is not None:
        # interrupt() requires checkpointed state to suspend/resume, so the
        # gate variant of the graph always compiles with MemorySaver.
        graph.add_node("human_gate", human_gate)
        graph.add_edge("human_gate", "tools")
        route_map[EDGE_GATE] = "human_gate"
    graph.add_conditional_edges("agent", should_continue, route_map)
    graph.add_edge("tools", "agent")
    if needs_approval is not None:
        return graph.compile(checkpointer=MemorySaver())
    return graph.compile()


# -- helpers ----------------------------------------------------------------------


def _run_script_out_path(args: dict[str, Any]) -> str | None:
    """run_script 始终把制品写到沙箱内的固定相对名 futures.md（见 builtin.run_script）。

    模型传入的 --out 会被忽略，故这里直接返回固定名，避免广播越界/绝对路径。
    仅用于 produced_file 事件，让 UI 能列出子进程真实落盘的文件。
    """
    return "futures.md"


def _normalize_decisions(
    resume_value: Any, pending: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Normalize a resume value into {call_id: decision} for the pending batch.

    Accepts either a bare "approve"/"deny" string (applied to every pending
    call) or a list of {"id", "action", "reason"?, "args"?} dicts. Decisions
    for unknown call ids are ignored - the caller may hold stale ids from an
    earlier round.
    """
    pending_ids = {c["id"] for c in pending}
    if isinstance(resume_value, str):
        action = resume_value if resume_value in {"approve", "deny"} else "deny"
        return {cid: {"id": cid, "action": action} for cid in pending_ids}
    decisions: dict[str, dict[str, Any]] = {}
    if isinstance(resume_value, dict):
        resume_value = [resume_value]
    for d in resume_value or []:
        if not isinstance(d, dict):
            continue
        cid = d.get("id")
        action = d.get("action")
        if cid in pending_ids and action in {"approve", "deny", "edit"}:
            decisions[cid] = d
    return decisions


def _to_llm_messages(history: list[Any]) -> list[dict[str, Any]]:
    """Convert state messages (LangChain objects or plain dicts) to LLM-ready dicts.

    Assistant messages that carried tool calls keep their tool_calls so the
    following tool results stay paired (required by OpenAI-compatible APIs).
    """
    out: list[dict[str, Any]] = []
    for m in history:
        if isinstance(m, AIMessage):
            d: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
            if getattr(m, "tool_calls", None):
                d["tool_calls"] = [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("args") or {}, ensure_ascii=False),
                        },
                    }
                    for tc in m.tool_calls
                ]
            out.append(d)
        elif isinstance(m, ToolMessage):
            # OpenAI-compatible APIs require tool messages to carry the id of
            # the assistant tool_call they answer (missing it -> json_parse_error)
            out.append(
                {
                    "role": "tool",
                    "content": m.content,
                    "tool_call_id": m.tool_call_id or "",
                }
            )
        elif isinstance(m, dict):
            role = m.get("role", "assistant")
            if role == "tool":
                d: dict[str, Any] = {"role": "tool", "content": str(m.get("content", ""))}
                if m.get("tool_call_id"):
                    d["tool_call_id"] = m["tool_call_id"]
                out.append(d)
            else:
                d = {"role": role, "content": str(m.get("content", ""))}
                if m.get("tool_calls"):
                    d["tool_calls"] = m["tool_calls"]
                out.append(d)
        else:  # HumanMessage / SystemMessage / others
            role = {"human": "user", "ai": "assistant", "system": "system"}.get(
                getattr(m, "type", ""), getattr(m, "type", "assistant")
            )
            out.append({"role": role, "content": m.content or ""})
    return out
