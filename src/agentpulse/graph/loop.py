"""The Loop: a LangGraph StateGraph wiring agent reasoning, tool execution,
and the routing decision that keeps the loop bounded.

    START ──> agent ──(has tool_calls & step<max)──> tools ──> agent
                 │
                 └──(no tool_calls or step>=max)──> END

Nodes are built as closures over the harness's router and tool registry so
the graph itself stays stateless and testable.

Messages inside the state are LangChain message objects (the `add_messages`
reducer normalizes them); the node boundaries below convert to/from the
OpenAI-shaped dicts that LiteLLM expects.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from ..llm import LiteLLMRouter
from ..tools.registry import ToolRegistry
from .state import AgentState

logger = logging.getLogger(__name__)

EDGE_TOOLS = "tools"
EDGE_END = "end"


def build_loop(
    router: LiteLLMRouter,
    registry: ToolRegistry,
    *,
    system_prompt: str,
    verbose: bool = False,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Build and compile the agent loop graph.

    Args:
        router: model router used by the agent node.
        registry: tools available to the agent.
        system_prompt: system message prepended to every completion.
        verbose: log each step to stderr.
        emit: optional callback fired on process events, so a task runner /
            UI can stream the loop's inner workings live:
            - ("thought", {"content": ...})       — agent reasoning text
            - ("tool_call", {"name", "args"})     — agent decided to call a tool
            - ("tool_result", {"name", "content"})— tool execution result
            Called from worker threads; must be thread-safe.

    Returns a callable that accepts an initial `AgentState` dict and returns
    the final state (with `messages` containing the full transcript).
    """

    # -- nodes ---------------------------------------------------------------

    def agent_node(state: AgentState) -> dict[str, Any]:
        step = state.get("step", 0) + 1
        history = state.get("messages", []) or []
        llm_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        llm_messages.extend(_to_llm_messages(history))

        response = router.complete(
            llm_messages,
            tools=registry.schemas() if len(registry) else None,
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
        tool_messages: list[ToolMessage] = []
        for tc in last.tool_calls:
            name = tc["name"]
            args = tc.get("args") or {}
            if verbose:
                logger.info("[tools] %s(%s)", name, args)
            try:
                result = registry.execute(name, args)
            except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
                result = f"Tool error: {exc}"
            if emit:
                emit("tool_result", {"name": name, "content": result, "step": state.get("step", 0)})
            tool_messages.append(ToolMessage(content=result, tool_call_id=tc.get("id") or ""))
        return {"messages": tool_messages}

    # -- routing ---------------------------------------------------------------

    def should_continue(state: AgentState) -> str:
        step = state.get("step", 0)
        max_steps = state.get("max_steps", 1)
        last = state["messages"][-1]
        has_calls = bool(getattr(last, "tool_calls", None))
        if has_calls and step < max_steps:
            return EDGE_TOOLS
        if has_calls:
            logger.warning(
                "[loop] hit max_steps=%s with pending tool calls; ending loop", max_steps
            )
        return EDGE_END

    # -- graph assembly ----------------------------------------------------------

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        should_continue,
        {EDGE_TOOLS: "tools", EDGE_END: END},
    )
    graph.add_edge("tools", "agent")
    return graph.compile()


# -- helpers ----------------------------------------------------------------------


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
            out.append({"role": "tool", "content": m.content})
        elif isinstance(m, dict):
            role = m.get("role", "assistant")
            if role == "tool":
                out.append({"role": "tool", "content": str(m.get("content", ""))})
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
