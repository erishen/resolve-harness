"""Built-in tools bundled with the harness.

These demonstrate the tool contract and are useful on their own:

- get_current_time  — wall clock (no network needed)
- add              — arithmetic, so the agent can "do math" reliably
- remember / recall — read & write long-term memory (the Memory pillar)

Extra tools can be registered on the harness with `h.tools.register(...)`.
"""

from __future__ import annotations

import datetime
from typing import Any

from ..memory import LongTermMemory
from .registry import ToolRegistry


def get_current_time() -> str:
    """Return the current local time as ISO 8601."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def add(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b


def _make_memory_tools(memory: LongTermMemory) -> list:
    def remember(key: str, value: Any, scope: str = "default") -> str:
        """Store a fact in long-term memory. Use for user preferences and facts worth keeping."""
        memory.remember(key, value, scope=scope)
        return f"stored '{key}' in scope '{scope}'"

    def recall(key: str, scope: str = "default") -> str:
        """Read a fact from long-term memory. Returns 'null' when absent."""
        value = memory.recall(key, scope=scope, default=None)
        return f"{key} = {value}" if value is not None else f"'{key}' not found in scope '{scope}'"

    def list_memories(scope: str = "default") -> str:
        """List all fact keys currently stored in the given scope."""
        rows = memory.search(scope=scope)
        if not rows:
            return f"no memories in scope '{scope}'"
        return "\n".join(f"- {r['key']}" for r in rows)

    return [remember, recall, list_memories]


def register_builtins(
    registry: ToolRegistry,
    memory: LongTermMemory,
    *,
    sandbox_dir: str | None = None,
) -> None:
    """Register the built-in toolset into a registry, wiring memory tools.

    Args:
        registry: target registry.
        memory: long-term memory the remember/recall tools bind to.
        sandbox_dir: optional sandbox root for filesystem tools
            (read_file / write_file / list_files). When None, fs tools are
            not registered.
    """
    registry.register(get_current_time)
    registry.register(add)
    for fn in _make_memory_tools(memory):
        registry.register(fn)
    if sandbox_dir is not None:
        from .fs import make_fs_tools

        for fn in make_fs_tools(sandbox_dir):
            registry.register(fn)
