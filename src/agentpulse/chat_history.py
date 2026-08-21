"""Persisted chat-turn usage log (SQLite).

Every completed chat turn (and approval resume) records how many tokens the
turn cost, so the UI can show "which turn, how many input/output tokens"
instead of only the live total. Same storage pattern as long_term.py:
stdlib sqlite3 + RLock + check_same_thread=False.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def default_chat_history_path() -> Path:
    """Usage log: <project root>/data/chat_history.db."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent / "data" / "chat_history.db"
    return Path.cwd() / "data" / "chat_history.db"


class ChatHistoryStore:
    """Append-only SQLite log of chat turns and their token usage."""

    def __init__(self, db_path: str | None = None, keep: int = 200) -> None:
        self.path = str(Path(db_path) if db_path else default_chat_history_path())
        self.keep = keep
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_msg TEXT NOT NULL,
                reply TEXT NOT NULL,
                steps INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.commit()

    def record(
        self,
        *,
        user_msg: str,
        reply: str,
        steps: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """Append one completed turn; trim the log to the newest `keep` rows."""
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO chat_turns"
                " (created_at, user_msg, reply, steps, prompt_tokens,"
                "  completion_tokens, total_tokens) VALUES (?,?,?,?,?,?,?)",
                (
                    created,
                    user_msg[:800],
                    reply[:2000],
                    int(steps),
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                ),
            )
            self._conn.execute(
                "DELETE FROM chat_turns WHERE id NOT IN"
                " (SELECT id FROM chat_turns ORDER BY id DESC LIMIT ?)",
                (self.keep,),
            )
            self._conn.commit()

    def recent(self, limit: int = 50) -> list[dict]:
        """Newest-first turns (usage numbers included)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, user_msg, reply, steps, prompt_tokens,"
                "       completion_tokens, total_tokens"
                " FROM chat_turns ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "created_at": r[0],
                "user_msg": r[1],
                "reply": r[2],
                "steps": r[3],
                "prompt_tokens": r[4],
                "completion_tokens": r[5],
                "total_tokens": r[6],
            }
            for r in rows
        ]

    def clear(self) -> int:
        """Delete all logged turns; returns the number of rows removed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM chat_turns")
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
