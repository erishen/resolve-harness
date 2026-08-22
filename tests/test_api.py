"""Tests for the FastAPI layer, using a duck-typed FakeHarness so no real
LLM / network is involved."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from resolve_harness.api import create_app
from resolve_harness.memory import LongTermMemory
from resolve_harness.tools.registry import ToolRegistry


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
            # TaskRunner._task_model / start() read these from the router.
            env_model="fake-model",
            resolve_model=lambda m=None: m or "fake-model",
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

    def run(self, text: str, **kwargs: Any) -> str:
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


@pytest.fixture(autouse=True)
def _isolate_deleted_examples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the example-tombstone store away from the real data/ dir."""
    target = tmp_path / "deleted_examples.json"
    monkeypatch.setattr("resolve_harness.examples._deleted_file", lambda: target)
    monkeypatch.setattr("resolve_harness.examples._cache", None)


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

    def test_examples_delete_hides_it(self, client: TestClient) -> None:
        # pick the first built-in example and delete it
        before = client.get("/api/examples").json()["examples"]
        target = before[0]
        res = client.post("/api/examples/delete", json=target)
        assert res.status_code == 200 and res.json() == {"ok": True}
        after = client.get("/api/examples").json()["examples"]
        assert target["text"] not in [e["text"] for e in after]
        # deleting again is a no-op
        again = client.post("/api/examples/delete", json=target)
        assert again.json() == {"ok": False}

    def test_examples_delete_requires_text(self, client: TestClient) -> None:
        res = client.post("/api/examples/delete", json={"label": "x", "text": ""})
        assert res.status_code == 422


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

    def test_list_sorted_by_mtime_desc(self, tmp_path: Path) -> None:
        import os
        import time as _time

        old = tmp_path / "old.txt"
        new = tmp_path / "new.txt"
        old.write_text("old", encoding="utf-8")
        new.write_text("new", encoding="utf-8")
        now = _time.time()
        os.utime(old, (now - 100, now - 100))  # older
        os.utime(new, (now, now))  # newer

        c = self._app_with_sandbox(tmp_path)
        files = c.get("/api/sandbox").json()["files"]
        assert [f["path"] for f in files] == ["new.txt", "old.txt"]

    def test_kind_classification(self, tmp_path: Path) -> None:
        samples = {
            "notes.md": "markdown",
            "page.html": "html",
            "photo.png": "image",
            "doc.pdf": "pdf",
            "data.csv": "csv",
            "config.json": "json",
            "script.py": "code",
            "style.css": "code",
            "readme.txt": "text",
            "archive.zip": "binary",
        }
        for name in samples:
            (tmp_path / name).write_bytes(b"x")
        c = self._app_with_sandbox(tmp_path)
        by_path = {f["path"]: f for f in c.get("/api/sandbox").json()["files"]}
        for name, kind in samples.items():
            assert by_path[name]["kind"] == kind, name
        assert by_path["photo.png"]["is_text"] is False
        assert by_path["notes.md"]["is_text"] is True
        assert by_path["script.py"]["is_text"] is True

    def test_raw_serves_bytes_with_content_type(self, tmp_path: Path) -> None:
        (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
        c = self._app_with_sandbox(tmp_path)
        res = c.get("/api/sandbox/raw?path=pic.png")
        assert res.status_code == 200
        assert res.content == b"\x89PNG\r\n\x1a\nfake-image"
        assert res.headers["content-type"].startswith("image/png")

    def test_raw_blocks_traversal(self, tmp_path: Path) -> None:
        c = self._app_with_sandbox(tmp_path)
        assert c.get("/api/sandbox/raw?path=../escape.txt").status_code == 404

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

    def test_clear_preserves_preexisting_files(self, tmp_path: Path) -> None:
        """清空只删 Agent 创建的文件；沙箱确立前已存在的「原有文件」必须保留，
        且默认不在列表中返回（preexisting=True 由前端隐藏）。"""
        c = self._app_with_sandbox(tmp_path)
        # 原有文件：在基线快照之后手工放置，等价于「切目录前就在」的用户文件
        h = c.app.state.harness
        h._sandbox_baseline = {"keep.txt", "docs/note.md"}
        (tmp_path / "keep.txt").write_text("user-data", encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "note.md").write_text("private", encoding="utf-8")
        # Agent 创建的文件
        (tmp_path / "made.txt").write_text("agent", encoding="utf-8")

        listing = {f["path"]: f for f in c.get("/api/sandbox").json()["files"]}
        assert listing["keep.txt"]["preexisting"] is True
        assert listing["docs/note.md"]["preexisting"] is True
        assert listing["made.txt"]["preexisting"] is False

        res = c.delete("/api/sandbox")
        assert res.status_code == 200
        assert res.json()["deleted"] == 1  # 只删了 Agent 创建的那一个
        # 原有文件原封不动
        assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "user-data"
        assert (tmp_path / "docs" / "note.md").read_text(encoding="utf-8") == "private"
        assert not (tmp_path / "made.txt").exists()

    def test_set_sandbox_dir_resnapshots_baseline(self, tmp_path: Path) -> None:
        """热切换沙箱根目录后，新目录里已有的文件即成为新的「原有文件」基线。"""
        from resolve_harness.harness import Harness

        dir_a, dir_b = tmp_path / "a", tmp_path / "b"
        dir_b.mkdir()
        (dir_b / "personal.txt").write_text("mine", encoding="utf-8")

        h = Harness(sandbox_dir=str(dir_a))
        assert h._sandbox_baseline == set()  # 空目录 → 无原有文件

        h.set_sandbox_dir(str(dir_b))
        assert h._sandbox_baseline == {"personal.txt"}
        # 切换后 Agent 新建的文件不属于基线
        h.tools.execute("write_file", {"path": "out.txt", "content": "x"})
        assert h._sandbox_baseline == {"personal.txt"}

    def test_set_sandbox_dir_rebinds_fs_tools(self, tmp_path: Path) -> None:
        from resolve_harness.harness import Harness

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        h = Harness(sandbox_dir=str(dir_a))
        h.tools.execute("write_file", {"path": "note.txt", "content": "in-a"})
        assert (dir_a / "note.txt").read_text(encoding="utf-8") == "in-a"

        # hot-switch the root directory
        h.set_sandbox_dir(str(dir_b))
        assert h.sandbox_dir == str(dir_b.resolve())
        h.tools.execute("write_file", {"path": "note.txt", "content": "in-b"})
        assert (dir_b / "note.txt").read_text(encoding="utf-8") == "in-b"
        # the previous root is untouched by the new root
        assert (dir_a / "note.txt").read_text(encoding="utf-8") == "in-a"
        # fs + run_script tools stay registered after the rebind
        for name in ("read_file", "write_file", "list_files", "run_script"):
            assert h.tools.get(name) is not None, name

    def test_sandbox_location_endpoint_records_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from resolve_harness.harness import Harness
        import resolve_harness.api as _api

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        saved: dict = {}
        monkeypatch.setattr("resolve_harness.api._save_config", lambda cfg: saved.update(cfg))
        # isolate create_app's config read so the history starts empty
        _real_cfg = _api._load_config()
        monkeypatch.setattr(
            "resolve_harness.api._load_config",
            lambda: {**_real_cfg, "sandbox_history": []},
        )

        h = Harness(sandbox_dir=str(dir_a))
        c = TestClient(create_app(harness=h))

        res = c.put("/api/sandbox/location", json={"path": str(dir_b)})
        assert res.status_code == 200
        body = res.json()
        assert body["sandbox_dir"] == str(dir_b.resolve())
        assert body["sandbox_history"] == [str(dir_b.resolve())]
        assert c.get("/api/sandbox").json()["sandbox_history"] == [str(dir_b.resolve())]
        # a write after switching lands in the new root
        c.put("/api/sandbox/content?path=switched.txt", json={"content": "x"})
        assert (dir_b / "switched.txt").read_text(encoding="utf-8") == "x"
        # persistence captured the new dir + history
        assert saved["sandbox_dir"] == str(dir_b.resolve())
        assert saved["sandbox_history"] == [str(dir_b.resolve())]

        # switching back dedupes and puts the latest first
        res2 = c.put("/api/sandbox/location", json={"path": str(dir_a)})
        assert res2.json()["sandbox_history"] == [
            str(dir_a.resolve()),
            str(dir_b.resolve()),
        ]

    def test_sandbox_location_default_sentinel_resets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from resolve_harness.harness import Harness, _default_sandbox_dir
        import resolve_harness.api as _api

        dir_a = tmp_path / "a"
        monkeypatch.setattr("resolve_harness.api._save_config", lambda cfg: None)
        _real_cfg = _api._load_config()
        monkeypatch.setattr(
            "resolve_harness.api._load_config",
            lambda: {**_real_cfg, "sandbox_history": []},
        )

        h = Harness(sandbox_dir=str(dir_a))
        c = TestClient(create_app(harness=h))
        res = c.put("/api/sandbox/location", json={"path": "__default__"})
        assert res.status_code == 200
        assert res.json()["sandbox_dir"] == str(_default_sandbox_dir().resolve())
        assert h.sandbox_dir == str(_default_sandbox_dir().resolve())

    def test_sandbox_location_empty_path_rejected(self, tmp_path: Path) -> None:
        from resolve_harness.harness import Harness

        h = Harness(sandbox_dir=str(tmp_path / "a"))
        c = TestClient(create_app(harness=h))
        assert c.put("/api/sandbox/location", json={"path": ""}).status_code == 422

    def test_sandbox_history_delete_and_clear(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from resolve_harness.harness import Harness
        import resolve_harness.api as _api

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        _real_cfg = _api._load_config()
        monkeypatch.setattr("resolve_harness.api._save_config", lambda cfg: None)
        monkeypatch.setattr(
            "resolve_harness.api._load_config",
            lambda: {**_real_cfg, "sandbox_history": []},
        )

        h = Harness(sandbox_dir=str(dir_a))
        c = TestClient(create_app(harness=h))
        c.put("/api/sandbox/location", json={"path": str(dir_b)})
        c.put("/api/sandbox/location", json={"path": str(dir_a)})
        assert [str(dir_a.resolve()), str(dir_b.resolve())] == [
            x for x in c.get("/api/sandbox").json()["sandbox_history"]
        ]

        # remove one entry (does not touch the current sandbox root)
        del_res = c.delete(
            "/api/sandbox/history", params={"path": str(dir_b.resolve())}
        )
        assert del_res.status_code == 200
        assert del_res.json()["sandbox_history"] == [str(dir_a.resolve())]

        # clear the rest
        all_res = c.delete("/api/sandbox/history/all")
        assert all_res.json()["sandbox_history"] == []


class TestChatHistory:
    """Completed chat turns persist token usage; history is queryable."""

    def test_done_turn_recorded_with_usage(self, client: TestClient) -> None:
        res = client.post("/api/chat", json={"message": "查一下汇率"})
        assert res.status_code == 200
        hist = client.get("/api/chat/history").json()["turns"]
        assert len(hist) >= 1
        newest = hist[0]
        assert newest["user_msg"] == "查一下汇率"
        assert newest["total_tokens"] >= 0
        assert "prompt_tokens" in newest and "completion_tokens" in newest

    def test_history_orders_newest_first(self, client: TestClient) -> None:
        client.post("/api/chat", json={"message": "第一轮"})
        client.post("/api/chat", json={"message": "第二轮"})
        turns = client.get("/api/chat/history").json()["turns"]
        assert turns[0]["user_msg"] == "第二轮"
        assert turns[1]["user_msg"] == "第一轮"

    def test_clear_history(self, client: TestClient) -> None:
        client.post("/api/chat", json={"message": "第一轮"})
        assert len(client.get("/api/chat/history").json()["turns"]) >= 1
        res = client.delete("/api/chat/history")
        assert res.status_code == 200
        assert res.json()["deleted"] >= 1
        assert client.get("/api/chat/history").json()["turns"] == []


class TestTools:
    def test_list_tools_with_modes(self, tmp_path: Path) -> None:
        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        h = Harness(memory_db=str(tmp_path / "m.db"))
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        res = c.get("/api/tools")
        assert res.status_code == 200
        tools = {t["name"]: t for t in res.json()["tools"]}
        # full builtin set is listed (no arithmetic tool — Fast Path covers it)
        assert {"get_current_time", "remember", "fetch", "write_file"} <= set(tools)
        assert "add" not in tools
        # chat subset flags
        assert tools["get_current_time"]["chat"] is True
        assert tools["remember"]["chat"] is True
        assert tools["write_file"]["chat"] is False   # removed from chat
        # task mode flags: may recall, never write/list memory
        assert tools["fetch"]["task"] is True
        assert tools["write_file"]["task"] is True
        assert tools["recall"]["task"] is True
        assert tools["remember"]["task"] is False
        assert tools["list_memories"]["task"] is False
        # approval flag survives
        assert tools["fetch"]["require_approval"] is True
        assert tools["get_current_time"]["require_approval"] is False
        h.close()


class TestPluginList:
    def test_plugins_include_builtin_core_and_promoted(self, tmp_path: Path) -> None:
        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        h = Harness(memory_db=str(tmp_path / "m.db"))
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        res = c.get("/api/plugins")
        assert res.status_code == 200
        plugs = res.json()["plugins"]
        names = [p["name"] for p in plugs]
        # core matchers always present
        assert "fastpath.arithmetic" in names
        assert "fastpath.time" in names
        # promoted detectors present
        assert any(n.startswith("detect_promoted") for n in names)
        # built-in ones are read-only flagged
        core = next(p for p in plugs if p["name"] == "fastpath.arithmetic")
        assert core["builtin"] is True and core["kind"] == "内置核心"
        h.close()

    def test_clear_memories(self, tmp_path: Path) -> None:
        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        h = Harness(memory_db=str(tmp_path / "m.db"))
        h.long_term.remember("k1", "v1", scope="default")
        h.long_term.remember("k2", "v2", scope="user")
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        # clear default scope only
        res = c.delete("/api/memories")  # no key -> clear default scope
        assert res.status_code == 200
        assert res.json()["deleted"] == 1
        assert c.get("/api/memories").json()["memories"] == []
        # user scope untouched
        assert c.get("/api/memories?scope=user").json()["memories"][0]["key"] == "k2"
        h.close()


class TestAgents:
    def test_agents_and_graph(self, tmp_path: Path) -> None:
        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        h = Harness(memory_db=str(tmp_path / "m.db"))
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        res = c.get("/api/agents")
        assert res.status_code == 200
        data = res.json()
        names = [a["name"] for a in data["agents"]]
        assert names == ["Planner", "Specialist", "Evaluator", "Reporter"]
        nodes = {n["id"] for n in data["graph"]["nodes"]}
        assert {"plan", "execute", "evaluate", "report", "end", "error"} <= nodes
        edges = data["graph"]["edges"]
        assert {"from", "to", "label"} <= set(edges[0])
        # the fail->replan loop edge exists
        assert any(e["from"] == "decision" and e["to"] == "plan" for e in edges)
        h.close()


class TestConfig:
    def test_parallel_get_set_persist(self, tmp_path: Path, monkeypatch) -> None:
        import resolve_harness.api as api_mod

        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        cfg = tmp_path / "config.json"
        monkeypatch.setattr(api_mod, "_config_path", lambda: cfg)
        h = Harness(memory_db=str(tmp_path / "m.db"))
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        # default (no config on disk -> 10 steps, fan-out 4, 1 replan round)
        cfg_res = c.get("/api/config").json()
        assert cfg_res["max_steps"] == 10
        assert cfg_res["parallel"] == 4
        assert cfg_res["max_replan_rounds"] == 1
        # update all
        r = c.put(
            "/api/config",
            json={"parallel": 3, "max_replan_rounds": 2, "max_steps": 15},
        )
        assert r.status_code == 200
        assert r.json()["max_steps"] == 15
        assert r.json()["parallel"] == 3 and r.json()["max_replan_rounds"] == 2
        # persisted to disk
        assert cfg.exists()
        assert '"max_steps": 15' in cfg.read_text(encoding="utf-8")
        # invalid rejected
        assert c.put("/api/config", json={"parallel": 0}).status_code == 422
        assert c.put("/api/config", json={"max_replan_rounds": 9}).status_code == 422
        assert c.put("/api/config", json={"max_steps": 0}).status_code == 422
        h.close()

    def test_agent_models_get_set_persist(self, tmp_path: Path, monkeypatch) -> None:
        import resolve_harness.api as api_mod

        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        cfg = tmp_path / "config.json"
        monkeypatch.setattr(api_mod, "_config_path", lambda: cfg)
        h = Harness(memory_db=str(tmp_path / "m.db"))
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        # empty by default
        assert c.get("/api/config").json()["agent_models"] == {}
        # set per-agent overrides; unknown keys dropped
        r = c.put(
            "/api/config",
            json={
                "parallel": 4,
                "max_replan_rounds": 1,
                "max_steps": 10,
                "agent_models": {
                    "planner": "openai/gpt-5",
                    "specialist": "openai/gpt-4o-mini",
                    "bogus": "x",
                },
            },
        )
        assert r.status_code == 200
        assert r.json()["agent_models"] == {
            "planner": "openai/gpt-5",
            "specialist": "openai/gpt-4o-mini",
        }
        # persisted
        assert '"planner": "openai/gpt-5"' in cfg.read_text(encoding="utf-8")
        # runtime: planner instance model updated
        assert h_runner_planner_model(c) == "openai/gpt-5"
        # clear by sending empty dict
        r = c.put(
            "/api/config",
            json={"parallel": 4, "max_replan_rounds": 1, "max_steps": 10, "agent_models": {}},
        )
        assert r.json()["agent_models"] == {}
        # cleared overrides fall back to the .env baseline (router.env_model),
        # not None - the planner model is always explicit.
        assert h_runner_planner_model(c) == h.router.env_model
        h.close()


def make_log_client(tmp_path: Path) -> tuple[TestClient, Any]:
    """Build an app with a real Harness + ChatHistoryStore writing to temp dirs."""
    from resolve_harness.chat_history import ChatHistoryStore
    from resolve_harness.harness import Harness

    h = Harness(memory_db=":memory:", sandbox_dir=str(tmp_path / "sb"))
    c = TestClient(
        create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
    )
    return c, h


class TestEvents:
    """The audit feature exposes a turn/task event journal via /api/events.

    EventLog isolation is handled by the autouse fixture in conftest.py, so the
    app built here writes to a temp DB — these tests also guard against a real
    data/event_log.db regression."""

    def _app_with_log(self, tmp_path: Path) -> tuple[TestClient, Any]:
        return make_log_client(tmp_path)

    def test_scope_without_ref_lists_refs_and_recent(self, tmp_path: Path) -> None:
        c, h = self._app_with_log(tmp_path)
        log = c.app.state.event_log
        log.append("chat", "r1", "thought", {"x": 1})
        log.append("chat", "r2", "thought", {"x": 2})
        res = c.get("/api/events?scope=chat")
        assert res.status_code == 200
        body = res.json()
        # refs newest-first, distinct per ref (r2 appended last)
        assert body["refs"] == ["r2", "r1"]
        assert len(body["events"]) == 2
        assert body["events"][0]["type"] == "thought"
        h.close()

    def test_ref_replays_single_thread(self, tmp_path: Path) -> None:
        c, h = self._app_with_log(tmp_path)
        log = c.app.state.event_log
        log.append("chat", "r1", "thought", {"i": 0})
        log.append("chat", "r1", "tool_call", {"i": 1})
        log.append("chat", "r2", "thought", {"i": 9})
        res = c.get("/api/events?scope=chat&ref=r1")
        assert res.status_code == 200
        body = res.json()
        # replay mode returns no refs list, only the requested thread in order
        assert body["refs"] == []
        assert [e["type"] for e in body["events"]] == ["thought", "tool_call"]
        h.close()

    def test_scope_filter_isolates_task_from_chat(self, tmp_path: Path) -> None:
        c, h = self._app_with_log(tmp_path)
        log = c.app.state.event_log
        log.append("chat", "r1", "thought", {})
        log.append("task", "t1", "plan", {})
        chat = c.get("/api/events?scope=chat").json()
        task = c.get("/api/events?scope=task").json()
        assert "r1" in chat["refs"] and "t1" in task["refs"]
        assert "t1" not in chat["refs"] and "r1" not in task["refs"]
        h.close()

    def test_limit_is_bounded(self, tmp_path: Path) -> None:
        c, h = self._app_with_log(tmp_path)
        assert c.get("/api/events?scope=chat&limit=0").status_code == 422
        assert c.get("/api/events?scope=chat&limit=10000").status_code == 422
        h.close()

    def test_bad_scope_rejected(self, tmp_path: Path) -> None:
        c, h = self._app_with_log(tmp_path)
        assert c.get("/api/events?scope=bogus").status_code == 422
        h.close()

    def test_fastpath_turn_is_logged_and_queryable(self, tmp_path: Path) -> None:
        """End-to-end: a real chat turn emits an event the endpoint returns."""
        c, h = self._app_with_log(tmp_path)
        res = c.post("/api/chat", json={"message": "23 加 45"})
        assert res.status_code == 200
        listing = c.get("/api/events?scope=chat").json()
        assert listing["refs"], "a chat turn should produce an audit ref"
        ref = listing["refs"][0]
        replay = c.get(f"/api/events?scope=chat&ref={ref}").json()
        assert any(e["type"] == "fastpath" for e in replay["events"])
        h.close()


class TestClear:
    """清空历史任务与事件日志的接口。"""

    def test_clear_events_removes_all(self, tmp_path: Path) -> None:
        c, h = make_log_client(tmp_path)
        log = c.app.state.event_log
        log.append("chat", "r1", "thought", {"x": 1})
        log.append("task", "t1", "plan", {"x": 2})
        res = c.delete("/api/events")
        assert res.status_code == 200
        assert res.json()["deleted"] == 2
        assert c.get("/api/events?scope=chat").json()["refs"] == []
        h.close()

    def test_clear_tasks_removes_all(self, tmp_path: Path) -> None:
        c, h = make_log_client(tmp_path)
        runner = c.app.state.runner
        # 往隔离后的临时持久化库插一条任务，验证清空接口
        runner._db().execute(
            "INSERT INTO task_history"
            " (task_id, objective, model, status, created_at, error, events)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("fake1", "demo", "m", "done", "2024-01-01T00:00:00", None, "[]"),
        )
        runner._conn.commit()
        runner._history = runner._load_history()  # 同步到内存，使 list() 可见
        assert any(t["task_id"] == "fake1" for t in c.get("/api/tasks").json()["tasks"])
        res = c.delete("/api/tasks")
        assert res.status_code == 200
        assert res.json()["deleted"] >= 1
        assert c.get("/api/tasks").json()["tasks"] == []
        h.close()


def h_runner_planner_model(client: TestClient) -> Any:
    """Fetch the app-state runner's planner model (runtime propagation check)."""
    return client.app.state.runner._planner.model


class TestDefaultModel:
    def test_default_model_set_clear_keep(self, tmp_path: Path, monkeypatch) -> None:
        import resolve_harness.api as api_mod

        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        cfg = tmp_path / "config.json"
        monkeypatch.setattr(api_mod, "_config_path", lambda: cfg)
        h = Harness(memory_db=str(tmp_path / "m.db"))
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        env_model = c.app.state.env_model
        assert env_model  # .env or default settings model

        # set default model -> active model switches live
        r = c.put(
            "/api/config",
            json={
                "parallel": 4,
                "max_replan_rounds": 1,
                "max_steps": 10,
                "default_model": "openai/gpt-5",
            },
        )
        assert r.status_code == 200
        assert r.json()["default_model"] == "openai/gpt-5"
        assert r.json()["active_model"] == "openai/gpt-5"
        assert '"default_model": "openai/gpt-5"' in cfg.read_text(encoding="utf-8")

        # agent_models untouched when omitted
        c.put(
            "/api/config",
            json={
                "parallel": 4,
                "max_replan_rounds": 1,
                "max_steps": 10,
                "agent_models": {"specialist": "openai/gpt-4o-mini"},
            },
        )
        assert c.get("/api/config").json()["agent_models"] == {"specialist": "openai/gpt-4o-mini"}
        assert c.get("/api/config").json()["default_model"] == "openai/gpt-5"

        # clear default -> back to env baseline
        r = c.put(
            "/api/config",
            json={
                "parallel": 4,
                "max_replan_rounds": 1,
                "max_steps": 10,
                "default_model": "",
            },
        )
        assert r.json()["default_model"] == ""
        assert r.json()["active_model"] == env_model
        h.close()


class TestModelProfiles:
    def test_profiles_set_resolve_keep(self, tmp_path: Path, monkeypatch) -> None:
        import resolve_harness.api as api_mod

        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        cfg = tmp_path / "config.json"
        monkeypatch.setattr(api_mod, "_config_path", lambda: cfg)
        h = Harness(memory_db=str(tmp_path / "m.db"))
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        assert c.get("/api/config").json()["models"] == {}

        r = c.put(
            "/api/config",
            json={
                "parallel": 4,
                "max_replan_rounds": 1,
                "max_steps": 10,
                "models": {
                    "fast": {
                        "base_url": "http://localhost:11434/v1",
                        "model": "openai/qwen2.5",
                        "api_key_env": "OPENAI_API_KEY",
                    }
                },
            },
        )
        assert r.status_code == 200
        assert r.json()["models"]["fast"]["model"] == "openai/qwen2.5"
        assert '"fast"' in cfg.read_text(encoding="utf-8")

        # runtime: router holds the profile and resolves it
        router = c.app.state.runner.router
        assert router.models["fast"]["base_url"] == "http://localhost:11434/v1"
        name, base, _key = router._resolve("fast")
        assert name == "openai/qwen2.5" and base == "http://localhost:11434/v1"
        # unknown alias passes through as a raw model string with settings defaults
        name, base, _key = router._resolve("nope")
        assert name == "nope" and base == router.settings.api_base

        # models untouched when omitted
        c.put(
            "/api/config",
            json={"parallel": 4, "max_replan_rounds": 1, "max_steps": 10},
        )
        assert c.get("/api/config").json()["models"]["fast"]["model"] == "openai/qwen2.5"
        h.close()

    def test_env_model_exposed(self, tmp_path: Path, monkeypatch) -> None:
        import resolve_harness.api as api_mod

        from resolve_harness.chat_history import ChatHistoryStore
        from resolve_harness.harness import Harness

        cfg = tmp_path / "config.json"
        monkeypatch.setattr(api_mod, "_config_path", lambda: cfg)
        h = Harness(memory_db=str(tmp_path / "m.db"))
        c = TestClient(
            create_app(harness=h, chat_history=ChatHistoryStore(str(tmp_path / "ch.db")))
        )
        env = c.get("/api/config").json()["env_model"]
        assert env["model"] == c.app.state.env_model
        assert env["api_key_env"] == "LLM_API_KEY"
        h.close()
