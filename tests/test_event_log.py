"""Unit tests for the append-only event log used by the audit feature.

The EventLog is the source of truth behind /api/events and the web AuditPanel.
These tests pin its write/read/trim semantics without any LLM or server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resolve_harness.event_log import EventLog


@pytest.fixture()
def log(tmp_path: Path) -> EventLog:
    return EventLog(db_path=str(tmp_path / "events.db"))


def _append_events(log: EventLog, scope: str, ref: str, count: int) -> None:
    for i in range(count):
        log.append(scope, ref, "step", {"i": i})


class TestEventLogAppend:
    def test_append_is_best_effort_and_returns_none(self, log: EventLog) -> None:
        # best-effort: must never raise on bad payloads, and has no meaningful
        # return value (callers ignore it).
        assert log.append("chat", "r1", "step", {"a": 1}) is None
        assert log.append("chat", "r1", "step", None) is None  # data defaults to {}

    def test_append_persists_across_reopen(self, tmp_path: Path) -> None:
        db = str(tmp_path / "events.db")
        EventLog(db_path=db).append("chat", "r1", "step", {"a": 1})
        rows = EventLog(db_path=db).replay("chat", "r1")
        assert len(rows) == 1
        assert rows[0]["data"]["a"] == 1


class TestEventLogReplay:
    def test_replay_returns_events_for_ref_in_order(self, log: EventLog) -> None:
        _append_events(log, "chat", "r1", 3)
        log.append("chat", "r2", "step", {"other": True})
        rows = log.replay("chat", "r1")
        assert [r["data"]["i"] for r in rows] == [0, 1, 2]

    def test_replay_other_scope_is_isolated(self, log: EventLog) -> None:
        log.append("chat", "r1", "step", {"a": 1})
        log.append("task", "r1", "step", {"a": 2})
        assert log.replay("chat", "r1")[0]["data"]["a"] == 1
        assert log.replay("task", "r1")[0]["data"]["a"] == 2

    def test_replay_shape_has_ts_type_data(self, log: EventLog) -> None:
        log.append("chat", "r1", "thought", {"k": 1})
        row = log.replay("chat", "r1")[0]
        assert {"ts", "type", "data"} <= set(row)
        assert row["type"] == "thought"


class TestEventLogRecent:
    def test_recent_honors_limit(self, log: EventLog) -> None:
        _append_events(log, "chat", "r1", 5)
        rows = log.recent("chat", 3)
        assert len(rows) == 3
        assert [r["data"]["i"] for r in rows] == [4, 3, 2]  # newest first

    def test_recent_scope_filter(self, log: EventLog) -> None:
        _append_events(log, "chat", "r1", 2)
        _append_events(log, "task", "t1", 2)
        rows = log.recent("task", 10)
        assert all(r["scope"] == "task" for r in rows)
        assert len(rows) == 2

    def test_recent_shape_has_scope_ref(self, log: EventLog) -> None:
        log.append("chat", "r1", "step", {"k": 1})
        row = log.recent("chat", 1)[0]
        assert row["scope"] == "chat" and row["ref_id"] == "r1"


class TestEventLogRefs:
    def test_refs_lists_distinct_refs_newest_first(self, log: EventLog) -> None:
        log.append("chat", "r1", "step", {})
        log.append("chat", "r2", "step", {})
        log.append("chat", "r1", "step", {})  # later activity on r1 bumps it
        assert log.refs("chat", 10) == ["r1", "r2"]

    def test_refs_empty_for_unknown_scope(self, log: EventLog) -> None:
        assert log.refs("task", 10) == []


class TestEventLogClear:
    def test_clear_removes_all_events(self, log: EventLog) -> None:
        _append_events(log, "chat", "r1", 3)
        _append_events(log, "task", "t1", 2)
        assert log.clear() == 5
        assert log.replay("chat", "r1") == []
        assert log.refs("chat", 10) == []
        assert log.recent("chat", 10) == []


class TestEventLogTrim:
    def test_trim_keeps_most_recent(self, tmp_path: Path) -> None:
        log = EventLog(db_path=str(tmp_path / "events.db"), keep=5)
        _append_events(log, "chat", "r1", 10)
        rows = log.replay("chat", "r1")
        # only the 5 newest survive (ids/values 5..9)
        assert [r["data"]["i"] for r in rows] == [5, 6, 7, 8, 9]

    def test_trim_off_when_keep_is_zero(self, tmp_path: Path) -> None:
        log = EventLog(db_path=str(tmp_path / "events.db"), keep=0)
        _append_events(log, "chat", "r1", 50)
        assert len(log.replay("chat", "r1")) == 50


class TestEventLogDataTypes:
    def test_data_round_trips_json(self, log: EventLog) -> None:
        payload = {"nested": {"a": [1, 2]}, "s": "hi", "b": True, "n": None}
        log.append("task", "t1", "eval", payload)
        assert log.replay("task", "t1")[0]["data"] == payload

    def test_oversized_payload_is_truncated_with_marker(self, log: EventLog) -> None:
        big = {"content": "x" * (20 * 1024)}  # 远超 16KB 上限
        log.append("chat", "r1", "thought", big)
        row = log.replay("chat", "r1")[0]["data"]
        assert row.get("truncated") is True
        assert "orig_bytes" in row and row["orig_bytes"] > 16 * 1024
        assert "preview" in row
