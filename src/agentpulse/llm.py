"""Model routing via LiteLLM: one interface, many providers.

The harness never talks to a provider SDK directly — it goes through
`litellm.completion`, which handles OpenAI/Anthropic/DeepSeek/Gemini/... and
any OpenAI-compatible endpoint (custom `api_base`). Switching models is just
a config change.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import litellm

from .config import Settings

logger = logging.getLogger(__name__)

# LiteLLM prints a lot of setup logging by default; keep it quiet unless asked.
litellm.suppress_debug_info = True
litellm.drop_params = True


class LLMError(RuntimeError):
    """Raised when the model call fails after retries."""


class LiteLLMRouter:
    """Thin wrapper around litellm.completion with retry + JSON-ish tool calls."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- public API ------------------------------------------------------

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Call the model. Returns the raw message dict from the provider.

        The message dict follows the OpenAI shape:
            {"role": "assistant", "content": ..., "tool_calls": [...]}
        """
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
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
