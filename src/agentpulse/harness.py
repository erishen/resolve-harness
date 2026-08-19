"""The Harness: the single public entry point that wires everything together.

    Harness
      ├── Settings          (.env / env vars → dataclass)
      ├── LiteLLMRouter     (model routing: one model string, many providers)
      ├── ShortTermMemory   (session transcript)
      ├── LongTermMemory    (SQLite facts, exposed to the agent as tools)
      ├── ToolRegistry      (built-ins + user tools)
      └── Loop              (compiled LangGraph StateGraph)

Typical usage:

    from agentpulse import Harness

    h = Harness()                      # reads LLM_MODEL / LLM_API_KEY from .env
    print(h.run("What time is it?"))   # may trigger tools under the hood
    print(h.run("Remember: I like dark theme"))  # persists via long-term memory
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .graph.loop import build_loop
from .llm import LiteLLMRouter
from .memory import LongTermMemory, ShortTermMemory
from .tools.builtin import register_builtins
from .tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


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
        if register_builtin_tools:
            if sandbox_dir is None:
                sandbox_dir = str(Path(__file__).resolve().parents[2] / "data" / "sandbox")
            register_builtins(self.tools, self.long_term, sandbox_dir=sandbox_dir)
        self.sandbox_dir = sandbox_dir

        self._loop = build_loop(
            self.router,
            self.tools,
            system_prompt=self.settings.system_prompt,
            verbose=self.settings.verbose,
        )
        # per-turn diagnostics, filled by run()
        self.last_trace: list[dict[str, Any]] = []
        self.last_steps: int = 0

    # -- tool registration (delegated) ---------------------------------------

    def register_tool(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Callable[..., Any]:
        """Register a tool on the harness's registry. Usable as a decorator."""
        return self.tools.register(
            func,
            name=name,
            description=description,
            parameters=parameters,
        )

    # -- the agent loop --------------------------------------------------------

    def run(self, text: str, *, system_prompt: str | None = None, max_steps: int | None = None) -> str:
        """Send one user message through the loop and return the final reply.

        The conversation is remembered across calls via short-term memory;
        facts the agent stores with `remember` persist via long-term memory.
        After the call, `self.last_trace` holds the tool-call events of this
        turn (for UI display), and `self.last_steps` the iteration count.

        Deterministic queries (arithmetic, current time) short-circuit through
        the fast path: answered by code, no LLM round-trip.
        """
        from .fastpath import try_fast_answer

        fast = try_fast_answer(text, sandbox_dir=self.sandbox_dir)
        if fast is not None:
            self.short_term.add("user", text)
            self.short_term.add("assistant", fast.answer)
            self.last_trace = [
                {"kind": "tool_call", "name": fast.method, "args": {}},
                {"kind": "tool_result", "name": fast.method, "content": fast.detail},
            ]
            self.last_steps = 1
            return fast.answer

        self.short_term.add("user", text)

        history = self.short_term.as_list()
        limit = self.settings.max_steps if max_steps is None else max_steps

        initial_state: dict[str, Any] = {
            "messages": history,
            "step": 0,
            "max_steps": max(1, limit),
        }
        final_state = self._loop.invoke(initial_state)

        transcript = final_state["messages"]
        final_reply = self._extract_final_reply(transcript)

        self.last_trace = self._extract_trace(transcript)
        self.last_steps = final_state["step"]

        if final_reply:
            self.short_term.add("assistant", final_reply)
        if self.settings.verbose:
            logger.info("[harness] steps=%s final_reply_len=%s", final_state["step"], len(final_reply))
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
    root = logging.getLogger("agentpulse")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    if os.getenv("HARNESS_LOG_LEVEL"):  # allow finer control
        root.setLevel(os.getenv("HARNESS_LOG_LEVEL", "").upper())
