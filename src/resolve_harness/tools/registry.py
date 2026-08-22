"""Tool registry: how the agent discovers and executes tools.

The registry is the harness's contract with the LLM:

- register a callable with a name, description, and a JSON Schema for its
  parameters;
- `schemas()` renders OpenAI-style function schemas to send to the model;
- `execute(name, args)` runs a tool and returns a string result — the only
  way tools are invoked, so the harness stays in full control of what runs
  (the "harness" guarantee).
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(RuntimeError):
    """Raised when a tool call fails at execution time."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    func: Callable[..., Any]
    tags: set[str] = field(default_factory=set)
    require_approval: bool = False

    def run(self, **kwargs: Any) -> str:
        """Execute and serialize the result to a string (what the LLM sees)."""
        try:
            result = self.func(**kwargs)
        except TypeError as exc:
            # Let the model see a precise error so it can retry with right args.
            raise ToolError(
                f"tool '{self.name}' called with wrong arguments: {exc}"
            ) from exc
        return self._serialize(result)

    def schema(self) -> dict[str, Any]:
        """OpenAI function-calling schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @staticmethod
    def _serialize(result: Any) -> str:
        if isinstance(result, str):
            return result
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)


class ToolRegistry:
    """A named collection of tools with duplicate-name protection."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration --------------------------------------------------------

    def register(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        tags: set[str] | None = None,
        require_approval: bool = False,
    ) -> Callable[..., Any]:
        """Register a tool; usable as `registry.register` or `@registry.register(...)`.

        When `parameters` is omitted, it is derived from the function
        signature: every non-optional parameter becomes a required string
        (annotations are respected when they're primitives).

        `require_approval=True` marks the tool as side-effectful: the agent
        loop will interrupt for a human decision before executing it
        (human-in-the-loop gate).
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            doc = (inspect.getdoc(fn) or "").strip()
            fallback = doc.splitlines()[0] if doc else f"Tool {fn.__name__}"
            tool = Tool(
                name=name or fn.__name__,
                description=description or fallback,
                parameters=parameters or self._schema_from_signature(fn),
                func=fn,
                tags=set(tags or ()),
                require_approval=require_approval,
            )
            if tool.name in self._tools:
                raise ValueError(f"tool '{tool.name}' is already registered")
            self._tools[tool.name] = tool
            return fn

        if func is not None:
            return decorator(func)
        return decorator

    # -- lookup & execution --------------------------------------------------

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def needs_approval(self, name: str) -> bool:
        """Whether the named tool is flagged for human approval (unknown -> False)."""
        tool = self._tools.get(name)
        return bool(tool and tool.require_approval)

    def approval_tools(self) -> list[str]:
        """Sorted names of all tools flagged for human approval."""
        return sorted(t.name for t in self._tools.values() if t.require_approval)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def execute(self, name: str, args: dict[str, Any] | None = None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name!r} (available: {', '.join(self.names())})")
        return tool.run(**(args or {}))

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    @staticmethod
    def _schema_from_signature(fn: Callable[..., Any]) -> dict[str, Any]:
        """Build a minimal JSON Schema from a function's type hints."""
        sig = inspect.signature(fn)
        properties: dict[str, Any] = {}
        required: list[str] = []
        type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
        for param_name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            ann = param.annotation
            ptype = type_map.get(ann, "string")
            if ptype == "string" and getattr(ann, "__origin__", None) is None and ann is not str:
                # treat enum-ish string unions as plain strings
                pass
            prop: dict[str, Any] = {"type": ptype}
            if param.default is not inspect.Parameter.empty:
                prop["default"] = param.default
            properties[param_name] = prop
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        return {"type": "object", "properties": properties, "required": required}
