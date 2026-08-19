"""Tests for multi-agent task orchestration: Planner -> Specialists ->
Evaluator -> Reporter, plus the SSE task API.

The real Harness is used with a scripted FakeRouter, so planning, execution
(with real tools) and evaluation all run offline.
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
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
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


# -- script builders ------------------------------------------------------------


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


def plan_json(*subtasks: tuple[str, str]) -> dict[str, Any]:
    subs = [
        {"title": title, "instruction": instruction, "artifacts": []}
        for title, instruction in subtasks
    ]
    return answer(json.dumps({"subtasks": subs}))


def verdict_json(passed: bool, score: int = 90, feedback: str = "", missing: list[str] | None = None) -> dict[str, Any]:
    return answer(
        json.dumps(
            {"passed": passed, "score": score, "feedback": feedback, "missing": missing or []}
        )
    )


def make_harness(script: list[dict[str, Any]]) -> Harness:
    h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir="/tmp/agentpulse-sandbox-test")
    h.router = FakeRouter(script)
    return h


def drain(task_id: str, runner: TaskRunner, timeout: float = 10.0) -> list[dict[str, Any]]:
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


def types_of(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


class TestOrchestration:
    def test_full_flow_two_subtasks(self) -> None:
        """Planner -> specialist0(tool loop) -> specialist1 -> evaluator(pass) -> reporter."""
        script = [
            plan_json(("计算", "用 add 计算 2+3"), ("写文件", "把结果写入 sandbox 的 result.txt")),
            tool_call("add", {"a": 2, "b": 3}),
            answer("计算结果为 5"),
            tool_call("write_file", {"path": "result.txt", "content": "5"}),
            answer("已写入 result.txt"),
            verdict_json(True, 95),
            answer("# 交付\n任务完成"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h)
        task_id = runner.start("完成一个计算并保存")
        events = drain(task_id, runner)
        types = types_of(events)

        assert types[0] == "task_start"
        assert types[-1] == "task_end"
        # orchestration phases in order
        assert types.index("plan") < types.index("subtask_start")
        assert types.index("evaluation") < types.index("task_end")
        assert "re_plan" not in types

        # events tagged with their subtask
        tool_call_ev = next(e for e in events if e["type"] == "tool_call")
        assert tool_call_ev["data"]["subtask"] == 0
        plan_ev = next(e for e in events if e["type"] == "plan")
        assert len(plan_ev["data"]["subtasks"]) == 2

        end = events[-1]
        assert end["data"]["passed"] is True
        assert end["data"]["score"] == 95
        assert runner.get(task_id)["status"] == "done"

    def test_replan_round_when_evaluator_fails(self) -> None:
        """Evaluator rejects -> re_plan with feedback -> second plan executes -> passes."""
        script = [
            plan_json(("写报告", "写一份简短报告")),
            answer("报告草稿完成"),
            verdict_json(False, 40, "缺少结论", ["conclusion"]),
            plan_json(("补结论", "给报告补充明确结论")),
            answer("已补充结论"),
            verdict_json(True, 85, "ok", []),
            answer("# 最终报告\n含结论"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h, max_replan_rounds=2)
        task_id = runner.start("写一份带结论的报告")
        events = drain(task_id, runner)
        types = types_of(events)

        assert types.count("plan") == 2
        assert types.count("evaluation") == 2
        assert "re_plan" in types
        re_plan_ev = next(e for e in events if e["type"] == "re_plan")
        assert "缺少结论" in re_plan_ev["data"]["feedback"]

        end = events[-1]
        assert end["type"] == "task_end"
        assert end["data"]["passed"] is True
        assert end["data"]["rounds"] == 2

    def test_subtask_artifacts_land_in_sandbox(self) -> None:
        script = [
            plan_json(("写文件", "写 hello.txt")),
            tool_call("write_file", {"path": "hello.txt", "content": "hi"}),
            answer("done"),
            verdict_json(True, 90),
            answer("# ok"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h)
        task_id = runner.start("写一个文件")
        events = drain(task_id, runner)
        results = [e["data"]["content"] for e in events if e["type"] == "tool_result"]
        assert any("已写入 hello.txt" in r for r in results)
        from pathlib import Path

        assert Path("/tmp/agentpulse-sandbox-test/hello.txt").read_text() == "hi"

    def test_late_subscriber_gets_full_history(self) -> None:
        script = [
            plan_json(("单步", "直接完成")),
            answer("done"),
            verdict_json(True, 88),
            answer("# deliverable"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h)
        task_id = runner.start("简单任务")
        drain(task_id, runner)
        events = drain(task_id, runner)  # subscribe after finish
        assert events[-1]["type"] == "task_end"
        assert events[0]["type"] == "task_start"

    def test_fast_path_skips_llm_entirely(self) -> None:
        """Deterministic objective short-circuits: the FakeRouter is never
        called — pure code answers "计算 2+3" in milliseconds."""
        h = make_harness([])  # empty script: any LLM call would yield an error event
        runner = TaskRunner(h)
        task_id = runner.start("计算 2+3")
        events = drain(task_id, runner)
        types = types_of(events)
        assert "error" not in types
        assert types[-1] == "task_end"
        end = events[-1]
        assert end["data"]["passed"] is True
        assert end["data"]["score"] == 100
        assert "5" in end["data"]["reply"]
        # task tree events still fire for the UI
        assert "plan" in types and "evaluation" in types
        # zero LLM round-trips
        assert h.router.calls == []

    def test_planner_failure_marks_error(self) -> None:
        class BoomRouter(FakeRouter):
            def complete(self, messages, tools=None, **kwargs):
                raise RuntimeError("provider down")

        h = make_harness([])
        h.router = BoomRouter([])
        runner = TaskRunner(h, max_steps=3)
        task_id = runner.start("会失败的任务")
        events = drain(task_id, runner)
        assert events[-1]["type"] == "error"
        assert "provider down" in events[-1]["data"]["message"]
        assert runner.get(task_id)["status"] == "error"


class TestTaskApi:
    def _client(self) -> TestClient:
        script = [
            plan_json(("计算", "1+1")),
            tool_call("add", {"a": 1, "b": 1}),
            answer("结果 2"),
            verdict_json(True, 92),
            answer("# 交付"),
        ]
        h = make_harness(script)
        return TestClient(create_app(harness=h, runner=TaskRunner(h, max_steps=4)))

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
        assert len(client.get("/api/tasks").json()["tasks"]) == 1

    def test_missing_task_404(self) -> None:
        assert self._client().get("/api/tasks/nope").status_code == 404

    def test_stream_sse_delivers_phases(self) -> None:
        client = self._client()
        task_id = client.post("/api/tasks", json={"objective": "1+1"}).json()["task_id"]
        with client.stream("GET", f"/api/tasks/{task_id}/stream") as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            text = "".join(r.iter_text())
        events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
        types = [e["type"] for e in events]
        assert types[0] == "task_start"
        assert "plan" in types and "subtask_start" in types and "evaluation" in types
        assert types[-1] == "task_end"
