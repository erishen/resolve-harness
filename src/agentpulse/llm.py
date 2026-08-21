"""Model routing via LiteLLM: one interface, many providers.

The harness never talks to a provider SDK directly — it goes through
`litellm.completion`, which handles OpenAI/Anthropic/DeepSeek/Gemini/... and
any OpenAI-compatible endpoint (custom `api_base`). Switching models is just
a config change.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Sequence
from typing import Any

import litellm

from .config import Settings

logger = logging.getLogger(__name__)

# LiteLLM prints a lot of setup logging by default; keep it quiet unless asked.
litellm.suppress_debug_info = True
litellm.drop_params = True

# keys we track per call (OpenAI-compatible usage shape)
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


class LLMError(RuntimeError):
    """Raised when the model call fails after retries."""


def _extract_usage(resp: Any) -> dict[str, int] | None:
    """Pull prompt/completion/total token counts from a litellm response.

    Returns None when the provider omitted usage (some providers/endpoints do).
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        data = usage
    else:  # OpenAI-style Usage object
        data = {k: getattr(usage, k, None) for k in _USAGE_KEYS}
    out: dict[str, int] = {}
    for k in _USAGE_KEYS:
        v = data.get(k)
        if isinstance(v, (int, float)):
            out[k] = int(v)
    return out or None


def usage_snapshot(router: Any) -> dict[str, int] | None:
    """Snapshot a router's cumulative token usage (None if it doesn't track)."""
    total = getattr(router, "total_usage", None)
    return dict(total) if total else None


def usage_diff(router: Any, baseline: dict[str, int] | None) -> dict[str, int] | None:
    """Tokens consumed since `baseline` (per-call diff). None when untracked."""
    total = getattr(router, "total_usage", None)
    if not total or not baseline:
        return None
    return {
        k: max(0, int(total.get(k, 0)) - int(baseline.get(k, 0)))
        for k in _USAGE_KEYS
    }


class LiteLLMRouter:
    """Thin wrapper around litellm.completion with retry + JSON-ish tool calls."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Token usage accounting: last call + cumulative (thread-safe, since
        # task specialists may call the router concurrently).
        self.last_usage: dict[str, int] | None = None
        self.total_usage: dict[str, int] = {k: 0 for k in _USAGE_KEYS}
        self._usage_lock = threading.Lock()

    # -- public API ------------------------------------------------------

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Call the model. Returns the raw message dict from the provider.

        The message dict follows the OpenAI shape:
            {"role": "assistant", "content": ..., "tool_calls": [...]}
        `model` overrides the router default per call (used for per-agent LLMs).
        """
        kwargs: dict[str, Any] = {
            "model": model or self.settings.model,
            "messages": [dict(m) for m in messages],
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": self.settings.max_tokens if max_tokens is None else max_tokens,
        }
        if self.settings.api_base:
            kwargs["api_base"] = self.settings.api_base
        if self.settings.api_key:
            kwargs["api_key"] = self.settings.api_key
        if tools:
            kwargs["tools"] = tools

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surface provider errors uniformly
            raise LLMError(f"LLM call failed ({self.settings.model}): {exc}") from exc

        message = resp.choices[0].message
        raw: dict[str, Any] = {"role": "assistant", "content": message.content}
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            raw["tool_calls"] = [
                {
                    "id": tc.id or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for i, tc in enumerate(tool_calls)
            ]

        usage = _extract_usage(resp)
        if usage:
            with self._usage_lock:
                self.last_usage = dict(usage)
                for k, v in usage.items():
                    self.total_usage[k] = self.total_usage.get(k, 0) + v
        return raw

    def parse_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize provider tool_calls into [{id, name, arguments(dict)}]."""
        calls: list[dict[str, Any]] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.get("id", ""), "name": name, "arguments": args})
        return calls
