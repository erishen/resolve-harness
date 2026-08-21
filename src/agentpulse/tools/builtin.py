"""Built-in tools bundled with the harness.

These demonstrate the tool contract and are useful on their own:

- get_current_time  — wall clock (no network needed)
- remember / recall — read & write long-term memory (the Memory pillar)

Arithmetic has no tool: pure math queries are resolved by the Fast Path
(code) before any model call, so an `add` tool would rarely fire.

Extra tools can be registered on the harness with `h.tools.register(...)`.
"""

from __future__ import annotations

import datetime
from typing import Any

from ..memory import LongTermMemory
from .registry import ToolRegistry


def get_current_time() -> str:
    """返回当前本地时间（ISO 8601）。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _make_memory_tools(memory: LongTermMemory) -> list:
    def remember(key: str, value: Any, scope: str = "default") -> str:
        """把事实存入长期记忆（用户偏好、关键结果）。"""
        memory.remember(key, value, scope=scope)
        return f"已存储 '{key}'（scope={scope}）"

    def recall(key: str, scope: str = "default") -> str:
        """从长期记忆读取事实（不存在时返回 null）。"""
        value = memory.recall(key, scope=scope, default=None)
        return f"{key} = {value}" if value is not None else f"在 scope '{scope}' 中未找到 '{key}'"

    def list_memories(scope: str = "default") -> str:
        """列出指定 scope 下的全部记忆 key。"""
        rows = memory.search(scope=scope)
        if not rows:
            return f"scope '{scope}' 中没有记忆"
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
    for fn in _make_memory_tools(memory):
        registry.register(fn)
    if sandbox_dir is not None:
        from .fs import make_fs_tools

        for fn in make_fs_tools(sandbox_dir):
            # write_file mutates the sandbox - route it through the
            # human-approval gate; read_file / list_files are side-effect free.
            registry.register(fn, require_approval=fn.__name__ == "write_file")
    from .http import fetch

    # fetch is the only network egress: a human should okay each request.
    registry.register(fetch, require_approval=True)
