"""Tests for task mode: TaskRunner event streaming + task API endpoints.

The real Harness is used but its router is swapped for a scripted FakeRouter,
so the full loop (including real tool execution) runs offline.
"""

from __future__ import annotations

import json
import queue
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentpulse.api import create_app
from agentpulse.harness import Harness
from agentpulse.tasks import TaskRunner


class FakeRouter:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)

    def complete(self, messages, tools=None, **kwargs):
        if not self.script:
            return {"role": "assistant", "content": "(out of script)"}
        return self.script.pop(0)

    def parse_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc["function"]
            try:
                args = json.loads(fn["arguments"])
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc["id"], "name": fn["name"], "arguments": args})
        return calls


def answer(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def make_harness(script: list[dict[str, Any]]) -> Harness:
    h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir="/tmp/agentpulse-sandbox-test")
    h.router = FakeRouter(script)
    return h


def drain(task_id: str, runner: TaskRunner, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Block on the task queue until the None sentinel; return events."""
    q = runner.subscribe(task_id)
    assert q is not None
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ev = q.get(timeout=1.0)
        except queue.Empty:
            continue
        if ev is None:
            break
        events.append(ev)
    else:
        raise TimeoutError("task did not finish in time")
    return events


class TestTaskRunner:
    def test_single_tool_task_event_sequence(self) -> None:
        h = make_harness([tool_call("add", {"a": 2, "b": 3}), answer("总和是 5")])
        runner = TaskRunner(h, max_steps=5)
        task_id = runner.start("计算 2+3")
        events = drain(task_id, runner)

        types = [e["type"] for e in events]
        assert types[0] == "task_start"
        assert "thought" in types or types[1] == "tool_call"
        assert types[-1] == "task_end"  # final event is always task_end
        # exact order: start -> (thought) -> tool_call -> tool_result -> (thought) -> task_end
        assert "tool_call" in types and "tool_result" in types
        assert types.index("tool_call") < types.index("tool_result") < types.index("task_end")

        tool_call_ev = next(e for e in events if e["type"] == "tool_call")
        assert tool_call_ev["data"]["name"] == "add"
        tool_result_ev = next(e for e in events if e["type"] == "tool_result")
        assert tool_result_ev["data"]["content"] == "5"  # add(2, 3)

        end = events[-1]
        assert end["data"]["reply"] == "总和是 5"
        assert runner.get(task_id)["status"] == "done"

    def test_write_file_tool_produces_artifact(self) -> None:
        h = make_harness(
            [
                tool_call("write_file", {"path": "notes/hello.txt", "content": "hello task mode"}),
                answer("已写入沙箱文件"),
            ]
        )
        runner = TaskRunner(h, max_steps=5)
        task_id = runner.start("在沙箱里写一个 hello.txt")
        events = drain(task_id, runner)

        results = [e["data"]["content"] for e in events if e["type"] == "tool_result"]
        assert any("written notes/hello.txt" in r for r in results)
        # file really exists on disk
        from pathlib import Path

        artifact = Path("/tmp/agentpulse-sandbox-test/notes/hello.txt")
        assert artifact.read_text(encoding="utf-8") == "hello task mode"

    def test_late_subscriber_gets_full_history(self) -> None:
        h = make_harness([answer("直接回答,不调工具")])
        runner = TaskRunner(h, max_steps=5)
        task_id = runner.start("简单的任务")
        drain(task_id, runner)  # let it finish
        events = drain(task_id, runner)  # subscribe again afterwards
        assert events[-1]["type"] == "task_end"
        assert events[0]["type"] == "task_start"

    def test_router_error_marks_task_error(self) -> None:
        class BoomRouter(FakeRouter):
            def complete(self, messages, tools=None, **kwargs):
                raise RuntimeError("provider down")

        h = make_harness([])
        h.router = BoomRouter([])
        runner = TaskRunner(h, max_steps=3)
        task_id = runner.start("会失败的任务")
        events = drain(task_id, runner)
        assert events[-1]["type"] == "error"
        assert runner.get(task_id)["status"] == "error"


class TestTaskApi:
    def _client(self) -> TestClient:
        h = make_harness(
            [tool_call("add", {"a": 1, "b": 1}), answer("结果 2")]
        )
        app = create_app(harness=h, runner=TaskRunner(h, max_steps=4))
        return TestClient(app)

    def test_create_and_snapshot(self) -> None:
        client = self._client()
        res = client.post("/api/tasks", json={"objective": "计算 1+1"})
        assert res.status_code == 200
        task_id = res.json()["task_id"]
        snap = client.get(f"/api/tasks/{task_id}").json()
        assert snap["status"] in ("running", "done")
        assert snap["objective"] == "计算 1+1"

    def test_list_tasks(self) -> None:
        client = self._client()
        client.post("/api/tasks", json={"objective": "x"})
        tasks = client.get("/api/tasks").json()["tasks"]
        assert len(tasks) == 1

    def test_missing_task_404(self) -> None:
        client = self._client()
        assert client.get("/api/tasks/nope").status_code == 404

    def test_stream_sse_delivers_full_sequence(self) -> None:
        client = self._client()
        task_id = client.post("/api/tasks", json={"objective": "1+1"}).json()["task_id"]
        with client.stream("GET", f"/api/tasks/{task_id}/stream") as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            text = "".join(r.iter_text())
        events = [
            json.loads(line[6:])
            for line in text.splitlines()
            if line.startswith("data: ")
        ]
        types = [e["type"] for e in events]
        assert types[0] == "task_start"
        assert "tool_call" in types and "tool_result" in types
        assert types[-1] == "task_end"
