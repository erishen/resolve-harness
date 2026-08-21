"""Long-term memory: durable, scope-scoped facts backed by SQLite.

This is the "remember across sessions" layer. The agent persists facts it
wants to keep (user preferences, decisions, learned context) via the
`remember` tool, and retrieves them via `recall`. Records are key/value
pairs with an optional scope (default "default") so you can partition by
user/agent/session without schema changes.

Storage is a single SQLite file (stdlib `sqlite3`, zero extra deps) created
lazily; the DB path can be pointed anywhere, e.g. a project-local
`data/memory.db` or an in-memory `:memory:` for tests.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class LongTermMemory:
    """SQLite-backed key/value fact store with scopes."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        # Default: <project_root>/data/memory.db
        if db_path is None:
            here = Path(__file__).resolve()
            for parent in here.parents:
                if (parent / "pyproject.toml").exists():
                    db_path = parent / "data" / "memory.db"
                    break
            else:
                db_path = Path.cwd() / "data" / "memory.db"
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Web servers (FastAPI) touch this from multiple threads: allow it and
        # guard every op with a lock. Single connection, serialized writes.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    # -- schema -------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    scope      TEXT NOT NULL,
                    key        TEXT NOT NULL,
                    value      TEXT NOT NULL,          -- JSON-encoded
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope, key)
                )
                """
            )
            self._conn.commit()

    # -- core ops -----------------------------------------------------------

    def remember(self, key: str, value: Any, scope: str = "default") -> None:
        """Upsert a fact. `value` can be any JSON-serializable object."""
        payload = json.dumps(value, ensure_ascii=False)
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memories (scope, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (scope, key, payload, now),
            )
            self._conn.commit()

    def recall(self, key: str, scope: str = "default", default: Any = None) -> Any:
        """Fetch one fact; returns `default` when absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM memories WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def search(self, scope: str | None = None, prefix: str = "") -> list[dict[str, Any]]:
        """List facts, optionally filtered by scope and key prefix.

        Returns rows as {"scope", "key", "value", "updated_at"} with decoded values.
        """
        sql = "SELECT scope, key, value, updated_at FROM memories"
        clauses: list[str] = []
        params: list[str] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if prefix:
            clauses.append("key LIKE ?")
            params.append(prefix + "%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "scope": r["scope"],
                "key": r["key"],
                "value": json.loads(r["value"]),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def forget(self, key: str, scope: str = "default") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM memories WHERE scope = ? AND key = ?", (scope, key)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def clear(self, scope: str | None = None) -> int:
        """Delete all memories (optionally limited to one scope)."""
        with self._lock:
            if scope is None:
                cur = self._conn.execute("DELETE FROM memories")
            else:
                cur = self._conn.execute("DELETE FROM memories WHERE scope = ?", (scope,))
            self._conn.commit()
        return cur.rowcount

    def count(self, scope: str | None = None) -> int:
        with self._lock:
            if scope is None:
                return self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE scope = ?", (scope,)
            ).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    def __repr__(self) -> str:
        return f"<LongTermMemory db={self.db_path} n={self.count()}>"
