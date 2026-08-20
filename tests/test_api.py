"""Tests for the FastAPI layer, using a duck-typed FakeHarness so no real
LLM / network is involved."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentpulse.api import create_app
from agentpulse.memory import LongTermMemory
from agentpulse.tools.registry import ToolRegistry


class FakeHarness:
    """Minimal stand-in exposing only the interface api.py relies on."""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(model="fake-model", max_steps=5, verbose=False)
        # TaskRunner is constructed by create_app even if unused by these tests;
        # give it duck-typed stand-ins.
        self.router = SimpleNamespace(
            complete=lambda *a, **k: {
                "role": "assistant",
                "content": '[{"label": "数据清洗", "text": "生成 12 个带噪声的温度读数保存为 temps.csv，写脚本清洗异常值并输出均值与最大值"}]',
            },
            parse_tool_calls=lambda m: [],
        )
        self.tools = ToolRegistry()
        self.sandbox_dir = None
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

    def test_examples_fixed(self, client: TestClient) -> None:
        res = client.get("/api/examples")
        assert res.status_code == 200
        examples = res.json()["examples"]
        assert all(e["source"] == "builtin" for e in examples)
        assert not any("记住" in e["text"] for e in examples)

    def test_examples_regenerate_returns_fresh(self, client: TestClient) -> None:
        res = client.post("/api/examples/regenerate")
        assert res.status_code == 200
        examples = res.json()["examples"]
        # built-ins are preserved, plus at least one model-generated task
        assert any(e["source"] == "builtin" for e in examples)
        assert any(e["source"] == "generated" for e in examples)


class TestSandbox:
    def _app_with_sandbox(self, tmp_path: Path) -> TestClient:
        h = FakeHarness()
        h.sandbox_dir = str(tmp_path)
        return TestClient(create_app(harness=h))

    def test_list_empty(self, client: TestClient) -> None:
        res = client.get("/api/sandbox")
        assert res.status_code == 200
        assert res.json()["files"] == []

    def test_list_and_read(self, tmp_path: Path) -> None:
        (tmp_path / "notes.md").write_text("hello sandbox", encoding="utf-8")
        c = self._app_with_sandbox(tmp_path)
        res = c.get("/api/sandbox")
        assert res.status_code == 200
        files = res.json()["files"]
        assert len(files) == 1 and files[0]["path"] == "notes.md"
        assert files[0]["is_text"] is True

        content = c.get("/api/sandbox/content?path=notes.md")
        assert content.status_code == 200
        assert content.json()["content"] == "hello sandbox"

    def test_read_blocks_traversal(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        res = c.get("/api/sandbox/content?path=../escape.txt")
        assert res.status_code == 404

    def test_write_and_read(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        put = c.put("/api/sandbox/content?path=notes.md", json={"content": "edited"})
        assert put.status_code == 200
        assert put.json()["size"] == len("edited")
        got = c.get("/api/sandbox/content?path=notes.md")
        assert got.json()["content"] == "edited"

    def test_write_creates_parent_dir(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        res = c.put("/api/sandbox/content?path=sub/deep/x.txt", json={"content": "hi"})
        assert res.status_code == 200
        assert (tmp_path / "sub" / "deep" / "x.txt").read_text(encoding="utf-8") == "hi"

    def test_write_blocks_traversal(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        res = c.put("/api/sandbox/content?path=../escape.txt", json={"content": "x"})
        assert res.status_code == 404

    def test_delete_file(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        res = c.delete("/api/sandbox/file?path=a.txt")
        assert res.status_code == 200 and res.json() == {"ok": True}
        assert not (tmp_path / "a.txt").exists()
        assert c.get("/api/sandbox").json()["files"] == []

    def test_delete_missing_is_404(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        assert c.delete("/api/sandbox/file?path=nope.txt").status_code == 404

    def test_delete_blocks_traversal(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        assert c.delete("/api/sandbox/file?path=../escape.txt").status_code == 404

    def test_clear(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "b.txt").write_text("b", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.txt").write_text("c", encoding="utf-8")
        res = c.delete("/api/sandbox")
        assert res.status_code == 200
        assert res.json()["deleted"] == 3
        assert c.get("/api/sandbox").json()["files"] == []
