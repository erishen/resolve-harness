"""Tests for the memory layers (short-term transcript + long-term SQLite)."""

from __future__ import annotations

import pytest

from agentpulse.memory import LongTermMemory, ShortTermMemory


# -- short-term -------------------------------------------------------------


class TestShortTermMemory:
    def test_add_and_read(self) -> None:
        mem = ShortTermMemory()
        mem.add("user", "hello")
        mem.add("assistant", "hi there")
        assert len(mem) == 2
        assert mem.as_list() == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    def test_trim_drops_oldest(self) -> None:
        mem = ShortTermMemory(max_messages=3)
        for i in range(5):
            mem.add("user", f"msg-{i}")
        assert len(mem) == 3
        contents = [m["content"] for m in mem.as_list()]
        assert contents == ["msg-2", "msg-3", "msg-4"]

    def test_last_filters_by_role(self) -> None:
        mem = ShortTermMemory()
        mem.add("user", "u1")
        mem.add("assistant", "a1")
        mem.add("user", "u2")
        assert mem.last("assistant")["content"] == "a1"
        assert mem.last()["content"] == "u2"

    def test_clear(self) -> None:
        mem = ShortTermMemory()
        mem.add("user", "x")
        mem.clear()
        assert len(mem) == 0
        assert mem.as_list() == []

    def test_add_many(self) -> None:
        mem = ShortTermMemory()
        mem.add_many([("user", "a"), ("assistant", "b")])
        assert len(mem) == 2


# -- long-term --------------------------------------------------------------


class TestLongTermMemory:
    @pytest.fixture()
    def mem(self, tmp_path) -> LongTermMemory:
        m = LongTermMemory(tmp_path / "memory.db")
        yield m
        m.close()

    def test_remember_recall_roundtrip(self, mem: LongTermMemory) -> None:
        mem.remember("color", {"name": "navy", "hex": "#000080"})
        assert mem.recall("color") == {"name": "navy", "hex": "#000080"}

    def test_recall_default_when_missing(self, mem: LongTermMemory) -> None:
        assert mem.recall("nope") is None
        assert mem.recall("nope", default="fallback") == "fallback"

    def test_upsert_overwrites(self, mem: LongTermMemory) -> None:
        mem.remember("k", "v1")
        mem.remember("k", "v2")
        assert mem.recall("k") == "v2"
        assert mem.count() == 1

    def test_scopes_are_isolated(self, mem: LongTermMemory) -> None:
        mem.remember("theme", "dark", scope="alice")
        mem.remember("theme", "light", scope="bob")
        assert mem.recall("theme", scope="alice") == "dark"
        assert mem.recall("theme", scope="bob") == "light"
        assert mem.count() == 2

    def test_search_filters_by_scope_and_prefix(self, mem: LongTermMemory) -> None:
        mem.remember("fav_color", "navy")
        mem.remember("fav_food", "sushi")
        mem.remember("secret", "x", scope="private")
        rows = mem.search(scope="default", prefix="fav_")
        assert sorted(r["key"] for r in rows) == ["fav_color", "fav_food"]
        assert all(r["scope"] == "default" for r in rows)
        assert len(mem.search(scope="private")) == 1

    def test_forget(self, mem: LongTermMemory) -> None:
        mem.remember("k", "v")
        assert mem.forget("k") is True
        assert mem.recall("k") is None
        assert mem.forget("k") is False

    def test_primitive_values(self, mem: LongTermMemory) -> None:
        mem.remember("count", 42)
        mem.remember("ok", True)
        mem.remember("note", "plain text")
        assert mem.recall("count") == 42
        assert mem.recall("ok") is True
        assert mem.recall("note") == "plain text"
