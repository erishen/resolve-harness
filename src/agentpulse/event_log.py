"""Append-only event log (SQLite): every agent-loop / task event persisted.

The harness's answer to deepseek-harness's event-sourced session log —
minus the framework.  Chat turns and task runs append their events here as
they happen (not as a final snapshot), so traces survive restarts and can be
replayed/audited.  Storage pattern matches long_term.py: stdlib sqlite3 +
RLock + check_same_thread=False; bounded by row count (`keep`).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 单条事件载荷上限：超出则截断并打标记，避免一条大 tool_result 撑爆日志库
MAX_PAYLOAD_BYTES = 16 * 1024


def default_event_log_path() -> Path:
    """Event log: <project root>/data/event_log.db."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent / "data" / "event_log.db"
    return Path.cwd() / "data" / "event_log.db"


class EventLog:
    """Append-only event journal.

    Rows: (scope, ref_id, ts, type, data_json).  `scope` names the event
    family ("chat" | "task"), `ref_id` the owning turn/task id, so a full
    trace is `replay(scope, ref_id)` in original order.
    """

    def __init__(self, db_path: str | None = None, keep: int = 5000) -> None:
        self.path = str(Path(db_path) if db_path else default_event_log_path())
        self.keep = keep
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                type TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_scope_ref ON events(scope, ref_id)"
        )
        self._conn.commit()

    def append(self, scope: str, ref_id: str, type_: str, data: dict | None = None) -> None:
        """Append one event; best-effort (logging must never break the agent)."""
        try:
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            payload = json.dumps(data or {}, ensure_ascii=False, default=str)
            if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                # 截断并保留预览，避免单条大事件撑爆日志库
                preview = payload[:2000]
                payload = json.dumps(
                    {
                        "truncated": True,
                        "orig_bytes": len(payload.encode("utf-8")),
                        "preview": preview,
                    },
                    ensure_ascii=False,
                )
            with self._lock:
                self._conn.execute(
                    "INSERT INTO events (scope, ref_id, ts, type, data) VALUES (?, ?, ?, ?, ?)",
                    (scope, ref_id, ts, type_, payload),
                )
                self._conn.commit()
                self._trim()
        except (sqlite3.Error, TypeError, ValueError) as exc:
            # 落库失败必须暴露，否则审计事件会静默丢失且无从排查
            logger.warning("[event_log] 事件落库失败 scope=%s ref=%s type=%s: %s", scope, ref_id, type_, exc)

    def replay(self, scope: str, ref_id: str) -> list[dict]:
        """A single turn/task's full trace, oldest-first (原始顺序)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, type, data FROM events WHERE scope = ? AND ref_id = ? ORDER BY id",
                (scope, ref_id),
            ).fetchall()
        out: list[dict] = []
        for ts, type_, data in rows:
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = {}
            out.append({"ts": ts, "type": type_, "data": parsed})
        return out

    def recent(self, scope: str | None = None, limit: int = 200) -> list[dict]:
        """Most recent events across a scope (or all), newest-first."""
        with self._lock:
            if scope:
                rows = self._conn.execute(
                    "SELECT scope, ref_id, ts, type, data FROM events"
                    " WHERE scope = ? ORDER BY id DESC LIMIT ?",
                    (scope, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT scope, ref_id, ts, type, data FROM events"
                    " ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {"scope": s, "ref_id": r, "ts": ts, "type": t, "data": _loads(d)}
            for s, r, ts, t, d in rows
        ]

    def refs(self, scope: str, limit: int = 50) -> list[str]:
        """Distinct ref ids for a scope, most recent first (供 UI 列出可回放项)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ref_id, MAX(id) AS m FROM events WHERE scope = ?"
                " GROUP BY ref_id ORDER BY m DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def _trim(self) -> None:
        """Bound the table: delete rows beyond `keep` (oldest ids)."""
        if self.keep <= 0:
            return
        row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        if row and row[0] > self.keep:
            excess = row[0] - self.keep
            self._conn.execute(
                "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id LIMIT ?)",
                (excess,),
            )

    def clear(self) -> int:
        """Delete all events; returns the number removed (best-effort)."""
        try:
            with self._lock:
                cur = self._conn.execute("DELETE FROM events")
                self._conn.commit()
                return cur.rowcount
        except sqlite3.Error:
            return 0


def _loads(data: str) -> dict:
    try:
        parsed = json.loads(data)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
