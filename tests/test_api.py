"""Tests for the FastAPI layer, using a duck-typed FakeHarness so no real
LLM / network is involved."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentpulse.api import create_app
from agentpulse.memory import LongTermMemory


class FakeHarness:
    """Minimal stand-in exposing only the interface api.py relies on."""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(model="fake-model")
        self.last_steps = 0
        self.last_trace: list[dict] = []
        self._transcript: list[dict] = []
        self.long_term = LongTermMemory(":memory:")
        self.memory_scope = "default"

    @property
    def transcript(self) -> list[dict]:
        return self._transcript

    def run(self, text: str) -> str:
        self._transcript.append({"role": "user", "content": text})
        self._transcript.append({"role": "assistant", "content": f"echo: {text}"})
        self.last_steps = 1
        self.last_trace = [
            {"kind": "tool_call", "name": "add", "args": {"a": 1, "b": 2}},
            {"kind": "tool_result", "name": "add", "content": "3"},
        ]
        return f"echo: {text}"

    def reset(self) -> None:
        self._transcript = []


@pytest.fixture()
def client() -> TestClient:
    app = create_app(harness=FakeHarness())
    return TestClient(app)


class TestApi:
    def test_health(self, client: TestClient) -> None:
        res = client.get("/api/health")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["model"] == "fake-model"

    def test_chat_reply_and_transcript(self, client: TestClient) -> None:
        res = client.post("/api/chat", json={"message": "hello"})
        assert res.status_code == 200
        body = res.json()
        assert body["reply"] == "echo: hello"
        assert body["steps"] == 1
        assert len(body["trace"]) == 2
        assert body["trace"][0]["kind"] == "tool_call"
        assert body["trace"][1]["kind"] == "tool_result"
        assert [m["role"] for m in body["transcript"]] == ["user", "assistant"]

    def test_chat_empty_message_rejected(self, client: TestClient) -> None:
        res = client.post("/api/chat", json={"message": ""})
        assert res.status_code == 422

    def test_state(self, client: TestClient) -> None:
        client.post("/api/chat", json={"message": "hi"})
        res = client.get("/api/state")
        body = res.json()
        assert len(body["transcript"]) == 2
        assert body["memories"] == []
        assert body["model"] == "fake-model"

    def test_reset(self, client: TestClient) -> None:
        client.post("/api/chat", json={"message": "hi"})
        res = client.post("/api/reset")
        assert res.json() == {"ok": True}
        assert client.get("/api/state").json()["transcript"] == []

    def test_memory_crud(self, client: TestClient) -> None:
        # write
        res = client.post("/api/memories", json={"key": "theme", "value": "dark"})
        assert res.json() == {"ok": True}
        # list
        rows = client.get("/api/memories").json()["memories"]
        assert len(rows) == 1 and rows[0]["key"] == "theme" and rows[0]["value"] == "dark"
        # delete
        res = client.delete("/api/memories?key=theme")
        assert res.json() == {"ok": True}
        assert client.get("/api/memories").json()["memories"] == []

    def test_memory_delete_missing_key(self, client: TestClient) -> None:
        res = client.delete("/api/memories?key=nope")
        assert res.json() == {"ok": False}

    def test_memory_scoped(self, client: TestClient) -> None:
        client.post("/api/memories", json={"key": "k", "value": 1, "scope": "alice"})
        default_rows = client.get("/api/memories").json()["memories"]
        assert default_rows == []
        alice_rows = client.get("/api/memories?scope=alice").json()["memories"]
        assert len(alice_rows) == 1
