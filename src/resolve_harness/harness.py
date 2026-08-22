"""The Harness: the single public entry point that wires everything together.

    Harness
      ├── Settings          (.env / env vars → dataclass)
      ├── LiteLLMRouter     (model routing: one model string, many providers)
      ├── ShortTermMemory   (session transcript)
      ├── LongTermMemory    (SQLite facts, exposed to the agent as tools)
      ├── ToolRegistry      (built-ins + user tools)
      └── Loop              (compiled LangGraph StateGraph)

Typical usage:

    from resolve_harness import Harness

    h = Harness()                      # reads LLM_MODEL / LLM_API_KEY from .env
    print(h.run("What time is it?"))   # may trigger tools under the hood
    print(h.run("Remember: I like dark theme"))  # persists via long-term memory
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from langgraph.types import Command

from .config import Settings
from .event_log import EventLog
from .graph.loop import build_loop
from .llm import LiteLLMRouter
from .memory import LongTermMemory, ShortTermMemory
from .tools.builtin import register_builtins
from .tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Tools the chat loop exposes. The full registry (self.tools) keeps the whole
# builtin set — the task runner copies from it — but interactive chat only
# needs time / long-term memory / fetch; file tools and arithmetic are either
# Fast-Path resolved or intentionally out of scope for chat.
CHAT_TOOL_NAMES = {"get_current_time", "remember", "recall", "fetch"}


def memory_hint(memory: LongTermMemory, scope: str, text: str) -> str:
    """Mechanism-level memory pre-fetch: find stored facts whose key overlaps
    the user's words.

    Instead of hoping the model decides to call `recall`, matching snapshots
    are injected directly into the conversation/system prompt — so
    "查一下美元兑人民币的汇率" surfaces the stored 19:32 snapshot before the
    model picks a tool. Matching is loose (shared 2-gram), since keys are
    human titles. Shared by chat (harness.run) and task specialists.
    """
    rows = memory.search(scope=scope)
    if not rows:
        return ""

    def bigrams(word: str) -> list[str]:
        return [word[i : i + 2] for i in range(len(word) - 1)]

    # tokens: chinese words (>=2 chars) + ascii words (>=3 chars)
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    tokens += re.findall(r"[A-Za-z0-9]{3,}", text.lower())

    hits: list[str] = []
    for r in rows:
        key = str(r["key"])
        key_lower = key.lower()
        overlap = False
        for t in tokens:
            if t in key_lower:
                overlap = True
                break
            if any(g in key_lower for g in bigrams(t)):
                overlap = True
                break
        if overlap:
            hits.append(f"- {key}: {str(r['value'])[:300]}")

    if not hits:
        return ""
    head = (
        "Relevant stored memories (long-term memory snapshots). "
        "Each snapshot carries its recorded time. If any snapshot answers the "
        "request and the user did NOT ask for the latest/real-time value, "
        "answer directly from the snapshot — do NOT call ANY tool for it "
        "(no get_current_time, no recall, no fetch):\n"
    )
    return head + "\n".join(hits)


class Harness:
    """Compose config + routing + memory + tools + loop into a runnable agent."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_steps: int | None = None,
        system_prompt: str | None = None,
        memory_db: str | None = None,
        memory_scope: str = "default",
        short_term_max: int = 40,
        register_builtin_tools: bool = True,
        sandbox_dir: str | None = None,
        verbose: bool | None = None,
    ) -> None:
        # Settings: explicit args win over .env, which wins over defaults.
        env = Settings()
        self.settings = Settings(
            model=model or env.model,
            api_base=api_base if api_base is not None else env.api_base,
            api_key=api_key if api_key is not None else env.api_key,
            temperature=env.temperature if temperature is None else temperature,
            max_tokens=env.max_tokens if max_tokens is None else max_tokens,
            max_steps=env.max_steps if max_steps is None else max_steps,
            system_prompt=system_prompt or env.system_prompt,
            verbose=env.verbose if verbose is None else verbose,
        )

        self.router = LiteLLMRouter(self.settings)

        self.short_term = ShortTermMemory(max_messages=short_term_max)
        self.long_term = LongTermMemory(memory_db)
        self.memory_scope = memory_scope

        self.tools = ToolRegistry()
        self.chat_tools = ToolRegistry()
        if register_builtin_tools:
            if sandbox_dir is None:
                sandbox_dir = str(Path(__file__).resolve().parents[2] / "data" / "sandbox")
            register_builtins(self.tools, self.long_term, sandbox_dir=sandbox_dir)
            # chat subset: only time / long-term memory / fetch
            for tool in self.tools:
                if tool.name in CHAT_TOOL_NAMES:
                    self.chat_tools.register(
                        tool.func,
                        name=tool.name,
                        description=tool.description,
                        parameters=tool.parameters,
                        require_approval=tool.require_approval,
                    )
        self.sandbox_dir = sandbox_dir

        # Human-in-the-loop: any tool flagged require_approval routes its
        # calls through the graph's approval gate (interrupt + resume).
        self._needs_approval: Callable[[str], bool] | None = None
        if self.chat_tools.approval_tools():
            self._needs_approval = self.chat_tools.needs_approval

        self._loop = build_loop(
            self.router,
            self.chat_tools,
            system_prompt=self.settings.system_prompt,
            verbose=self.settings.verbose,
            needs_approval=self._needs_approval,
            emit=self._on_loop_event,
        )
        # 事件日志：聊天循环事件实时落库（scope="chat", ref=turn thread_id）
        self._event_log = EventLog()
        # 当前 turn 的日志引用，由 run() 设置
        self._log_ref: str | None = None
        # per-turn diagnostics, filled by run()
        self.last_trace: list[dict[str, Any]] = []
        self.last_steps: int = 0
        self._last_memory_hits: list[str] = []
        # set while the loop is suspended on an approval interrupt:
        # {"thread_id": str, "tool_calls": [{"id", "name", "args"}, ...]}
        self.pending_approval: dict[str, Any] | None = None

    # -- tool registration (delegated) ---------------------------------------

    def _on_loop_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Loop emit sink：把单循环事件持久化到事件日志（scope="chat"）。"""
        self._event_log.append("chat", self._log_ref or "?", event_type, data)

    def register_tool(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        require_approval: bool = False,
    ) -> Callable[..., Any]:
        """Register a tool on the harness's registry. Usable as a decorator.

        `require_approval=True` routes the tool's calls through the
        human-approval gate; the loop is rebuilt so the flag takes effect
        even for tools registered after construction.
        """

        def _maybe_enable_gate() -> None:
            if require_approval and self._needs_approval is None:
                self._needs_approval = self.chat_tools.needs_approval
                self._loop = build_loop(
                    self.router,
                    self.chat_tools,
                    system_prompt=self.settings.system_prompt,
                    verbose=self.settings.verbose,
                    needs_approval=self._needs_approval,
                    emit=self._on_loop_event,
                )

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            # full registry (task runner copies from it) + chat registry
            self.tools.register(
                fn,
                name=name,
                description=description,
                parameters=parameters,
                require_approval=require_approval,
            )
            self.chat_tools.register(
                fn,
                name=name,
                description=description,
                parameters=parameters,
                require_approval=require_approval,
            )
            _maybe_enable_gate()
            return fn

        if func is not None:
            decorator(func)
            return func
        return decorator

    # -- the agent loop --------------------------------------------------------

    def _memory_hint(self, text: str) -> str:
        """Find stored facts whose key overlaps the user's words (chat mode)."""
        return memory_hint(self.long_term, self.memory_scope, text)

    def run(
        self,
        text: str,
        *,
        system_prompt: str | None = None,
        max_steps: int | None = None,
        session_history: bool = True,
        model: str | None = None,
    ) -> str:
        """Send one user message through the loop and return the final reply.

        The conversation is remembered across calls via short-term memory;
        facts the agent stores with `remember` persist via long-term memory.
        After the call, `self.last_trace` holds the tool-call events of this
        turn (for UI display), and `self.last_steps` the iteration count.

        `model`: session-level model override for THIS turn (alias or model
        name). None falls back to the loop default (chat model). The override
        reaches every agent-node LLM call of this turn via the graph state —
        the same resolution as the router default otherwise applies.

        Deterministic queries (arithmetic, current time) short-circuit through
        the fast path: answered by code, no LLM round-trip.

        Human-in-the-loop: when the loop hits an approval-gated tool call it
        suspends. run() then returns "" and `self.pending_approval` holds the
        waiting calls; the caller resolves them with `resolve_approval()`.

        `session_history=False` sends ONLY the current user message to the
        model (plus any memory hints) — no past turns. The transcript still
        accumulates for display; cross-turn knowledge comes from long-term
        memory (remember/recall + the automatic memory pre-fetch).
        """
        from .fastpath import try_fast_answer

        # A dangling approval from an earlier turn means its caller walked
        # away: deny it so the transcript stays consistent before a new turn.
        if self.pending_approval is not None:
            self._auto_deny_pending()

        fast = try_fast_answer(text, sandbox_dir=self.sandbox_dir)
        if fast is not None:
            self._log_ref = uuid.uuid4().hex[:12]
            self._event_log.append(
                "chat", self._log_ref, "fastpath", {"method": fast.method, "answer": fast.answer[:200]}
            )
            self.short_term.add("user", text)
            self.short_term.add("assistant", fast.answer)
            self.last_trace = [
                {"kind": "tool_call", "name": fast.method, "args": {}},
                {"kind": "tool_result", "name": fast.method, "content": fast.detail},
            ]
            self.last_steps = 1
            return fast.answer

        # codegen: let the model write a detector once; persist on success
        from .codegen import codegen_solve

        gen_answer = codegen_solve(self.router, text, model=model)
        if gen_answer is not None:
            self._log_ref = uuid.uuid4().hex[:12]
            self._event_log.append(
                "chat", self._log_ref, "codegen", {"answer": gen_answer[:200]}
            )
            self.short_term.add("user", text)
            self.short_term.add("assistant", gen_answer)
            self.last_trace = [
                {"kind": "tool_call", "name": "codegen", "args": {}},
                {"kind": "tool_result", "name": "codegen", "content": gen_answer[:200]},
            ]
            self.last_steps = 1
            return gen_answer

        self.short_term.add("user", text)

        if session_history:
            history = self.short_term.as_list()
        else:
            # chat mode: only this turn reaches the model (transcript display
            # is unaffected — short_term still accumulates above)
            history = [{"role": "user", "content": text}]
        hint = self._memory_hint(text)
        if hint:
            # inject matching snapshots as a system message, ahead of the turn
            history = [{"role": "system", "content": hint}] + history
            # surface the pre-fetch in the tool trace so the UI shows WHY the
            # agent "knows" the snapshot without calling recall
            self._last_memory_hits = re.findall(r"^- (.+?): ", hint, re.M)
        else:
            self._last_memory_hits = []
        limit = self.settings.max_steps if max_steps is None else max_steps

        initial_state: dict[str, Any] = {
            "messages": history,
            "step": 0,
            "max_steps": max(1, limit),
            "model": model,  # 会话级模型覆盖，agent 节点优先使用
        }
        # A fresh thread per turn: short-term memory stays the single source
        # of history, so the checkpointer never accumulates it twice.
        thread_id = uuid.uuid4().hex
        # 事件日志引用：本 turn 的循环事件全部归属该 ref（可 replay）
        self._log_ref = thread_id
        if self._needs_approval is not None:
            final_state = self._loop.invoke(
                initial_state, {"configurable": {"thread_id": thread_id}}
            )
        else:
            final_state = self._loop.invoke(initial_state)

        if self._record_pending(final_state, thread_id):
            return ""

        reply = self._finish_turn(final_state)
        if self._last_memory_hits:
            # show the memory pre-fetch as the first trace event
            self.last_trace = [
                {"kind": "tool_call", "name": "memory_hint", "args": {"matched": self._last_memory_hits}},
            ] + self.last_trace
        return reply

    def resolve_approval(self, decisions: Any) -> str:
        """Resume a suspended turn with the human's approval decisions.

        Args:
            decisions: a bare "approve"/"deny" string, or a list of
                {"id", "action": "approve"|"deny"|"edit", "reason"?, "args"?}.

        Returns the final reply - or "" if the resumed turn suspends again on
        a later approval-gated call (`pending_approval` is updated then).
        """
        if self.pending_approval is None:
            raise RuntimeError("no approval is pending")
        config = {"configurable": {"thread_id": self.pending_approval["thread_id"]}}
        final_state = self._loop.invoke(Command(resume=decisions), config)

        if self._record_pending(final_state, self.pending_approval["thread_id"]):
            return ""

        return self._finish_turn(final_state)

    # -- approval internals -------------------------------------------------------

    def _record_pending(self, final_state: dict[str, Any], thread_id: str) -> bool:
        """If the loop suspended on an approval interrupt, stash the pending
        calls on self.pending_approval; returns True when suspended."""
        interrupts = final_state.get("__interrupt__")
        if not interrupts:
            self.pending_approval = None
            return False
        payload = getattr(interrupts[0], "value", None) or {}
        self.pending_approval = {
            "thread_id": thread_id,
            "tool_calls": payload.get("tool_calls", []),
        }
        self.last_trace = self._extract_trace(final_state.get("messages", []))
        self.last_steps = final_state.get("step", 0)
        return True

    def _auto_deny_pending(self) -> None:
        """Close a dangling approval by denying it; never raises into the
        caller's new turn."""
        try:
            self.resolve_approval("deny")
        except Exception as exc:  # noqa: BLE001 - best effort cleanup
            logger.warning("[harness] auto-deny of pending approval failed: %s", exc)
            self.pending_approval = None

    def _finish_turn(self, final_state: dict[str, Any]) -> str:
        """Extract the reply/trace from a completed turn and persist it."""
        transcript = final_state["messages"]
        final_reply = self._extract_final_reply(transcript)

        self.last_trace = self._extract_trace(transcript)
        self.last_steps = final_state.get("step", 0)

        if final_reply:
            self.short_term.add("assistant", final_reply)
        if self.settings.verbose:
            logger.info("[harness] steps=%s final_reply_len=%s", final_state.get("step", 0), len(final_reply))
        return final_reply

    def ask(self, text: str, **kwargs: Any) -> str:
        """Alias for run()."""
        return self.run(text, **kwargs)

    def reset(self) -> None:
        """Clear short-term memory (keeps long-term facts)."""
        self.short_term.clear()

    # -- introspection -----------------------------------------------------------

    @property
    def transcript(self) -> list[dict[str, str]]:
        """The current session transcript (user/assistant pairs)."""
        return self.short_term.as_list()

    def remember(self, key: str, value: Any) -> None:
        """Store a fact directly (outside the agent loop)."""
        self.long_term.remember(key, value, scope=self.memory_scope)

    def recall(self, key: str, default: Any = None) -> Any:
        """Read a fact directly (outside the agent loop)."""
        return self.long_term.recall(key, scope=self.memory_scope, default=default)

    def close(self) -> None:
        """Release resources (close the SQLite connection)."""
        self.long_term.close()

    def __enter__(self) -> "Harness":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _extract_final_reply(messages: list[Any]) -> str:
        """Last non-tool assistant message content, falling back gracefully."""
        for m in reversed(messages):
            if isinstance(m, dict):
                if m.get("role") == "assistant" and m.get("content"):
                    return str(m["content"])
            elif getattr(m, "type", "") == "ai" and m.content:
                return str(m.content)
        return ""

    @staticmethod
    def _extract_trace(messages: list[Any]) -> list[dict[str, Any]]:
        """Flatten tool-call / tool-result pairs into UI-friendly events."""
        from langchain_core.messages import AIMessage, ToolMessage

        pending: dict[str, str] = {}
        trace: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, AIMessage):
                for tc in getattr(m, "tool_calls", None) or []:
                    pending[tc.get("id", "")] = tc["name"]
                    trace.append(
                        {
                            "kind": "tool_call",
                            "name": tc["name"],
                            "args": tc.get("args") or {},
                        }
                    )
            elif isinstance(m, ToolMessage):
                trace.append(
                    {
                        "kind": "tool_result",
                        "name": pending.pop(m.tool_call_id, "tool"),
                        "content": str(m.content),
                    }
                )
        return trace


def _configure_logging(verbose: bool) -> None:
    """Best-effort stderr logging setup; never crash if already configured."""
    if verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    root = logging.getLogger("resolve_harness")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    if os.getenv("HARNESS_LOG_LEVEL"):  # allow finer control
        root.setLevel(os.getenv("HARNESS_LOG_LEVEL", "").upper())
