"""Short-term memory: the in-session message transcript.

Kept as plain dicts (OpenAI-ish shape) so it stays provider-agnostic and
easy to serialize; the LLM router converts them to whatever the provider
expects. Supports trimming to a max length so long sessions don't blow up
the context window.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator, MutableSequence
from typing import Any


class ShortTermMemory:
    """A bounded FIFO transcript of {role, content} messages for one session."""

    def __init__(self, max_messages: int = 40, *, history: list[dict[str, Any]] | None = None) -> None:
        self.max_messages = max(2, max_messages)
        self._messages: list[dict[str, Any]] = list(history or [])

    # -- core ops ----------------------------------------------------------

    def add(self, role: str, content: str) -> None:
        """Append a message and trim the oldest if over the cap."""
        self._messages.append({"role": role, "content": content, "ts": self._now()})
        if len(self._messages) > self.max_messages:
            # keep newest N messages; never trim the very first user turn
            overflow = len(self._messages) - self.max_messages
            del self._messages[:overflow]

    def add_many(self, pairs: list[tuple[str, str]]) -> None:
        for role, content in pairs:
            self.add(role, content)

    def as_list(self) -> list[dict[str, Any]]:
        """Messages without timestamps — ready to hand to the LLM router."""
        return [{"role": m["role"], "content": m["content"]} for m in self._messages]

    def last(self, role: str | None = None) -> dict[str, Any] | None:
        for m in reversed(self._messages):
            if role is None or m["role"] == role:
                return m
        return None

    def clear(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._messages)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self._messages[index]

    def __repr__(self) -> str:
        return f"<ShortTermMemory n={len(self._messages)} max={self.max_messages}>"

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
