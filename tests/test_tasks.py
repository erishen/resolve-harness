"""Tests for multi-agent task orchestration: Planner -> Specialists ->
Evaluator -> Reporter, plus the SSE task API.

The real Harness is used with a scripted FakeRouter, so planning, execution
(with real tools) and evaluation all run offline.
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from resolve_harness.api import create_app
from resolve_harness.harness import Harness
from resolve_harness.tasks import TaskRecord, TaskRunner

# TaskRunner 的历史库与沙箱：每个测试用 tmp_path 隔离，杜绝跨测试/跨用户的
# /tmp 污染（pytest 的 tmp_path 按测试唯一，且自动清理）。默认值仅在未触发
# autouse fixture 的极少数场景兜底。
_SHARED_HISTORY = Path("/tmp/resolve_harness-task-hist-test.db")
_SANDBOX = "/tmp/resolve_harness-sandbox-test"


@pytest.fixture(autouse=True)
def _task_env(tmp_path) -> None:
    global _SHARED_HISTORY, _SANDBOX
    _SHARED_HISTORY = tmp_path / "hist.db"
    _SANDBOX = tmp_path / "sandbox"
    yield


class FakeRouter:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        # TaskRunner._task_model reads the .env baseline from the router.
        self.env_model = "fake-model"

    def resolve_model(self, model: str | None = None) -> str:
        return model or self.env_model

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
    h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(_SANDBOX))
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
            # note: instruction must NOT be a pure arithmetic expression, or the
            # subtask Fast Path would short-circuit it and consume no script
            plan_json(("计算", "完成一个算术计算任务并报告结果"), ("写文件", "把结果写入 sandbox 的 result.txt")),
            tool_call("add", {"a": 2, "b": 3}),
            answer("计算结果为 5"),
            tool_call("write_file", {"path": "result.txt", "content": "5"}),
            answer("已写入 result.txt"),
            verdict_json(True, 95),
            answer("# 交付\n任务完成"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h, parallel=1, history_path=str(_SHARED_HISTORY), codegen=False)
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
        runner = TaskRunner(h, parallel=1, history_path=str(_SHARED_HISTORY), max_replan_rounds=2, codegen=False)
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
        runner = TaskRunner(h, parallel=1, history_path=str(_SHARED_HISTORY), codegen=False)
        task_id = runner.start("写一个文件")
        events = drain(task_id, runner)
        results = [e["data"]["content"] for e in events if e["type"] == "tool_result"]
        assert any("已写入 hello.txt" in r for r in results)
        from pathlib import Path

        # write_file 落在「当前任务」沙箱内（而非全局沙箱根）
        assert (Path(_SANDBOX) / "tasks" / task_id / "hello.txt").read_text() == "hi"

    def test_cha_qihuo_run_script_flow(self) -> None:
        """查期货示例：specialist 调用 run_script fetch_shfe_futures，脚本在「当前任务」
        沙箱内抓取上期所行情并写出 futures.md，任务最终通过评估并交付。"""
        import urllib.request
        from pathlib import Path

        try:
            urllib.request.urlopen("https://www.shfe.com.cn", timeout=5)
        except Exception:
            pytest.skip("no network to shfe.com.cn")

        script = [
            plan_json(("查期货", "调用 run_script 运行 fetch_shfe_futures 获取上期所前5大涨幅合约")),
            tool_call("run_script", {"name": "fetch_shfe_futures"}),
            answer("已运行 run_script 获取行情，详见 futures.md"),
            verdict_json(True, 95),
            answer("# 交付\n上期所前5大涨幅合约见 futures.md"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h, parallel=1, history_path=str(_SHARED_HISTORY), codegen=False, max_steps=8)
        task_id = runner.start("获取上海期货交易所每日行情并保存为 futures.md")
        events = drain(task_id, runner)
        results = [e["data"]["content"] for e in events if e["type"] == "tool_result"]
        # 非交易日（周末/休市）上期所不发布日行情，数据源 404/失败属环境性，跳过。
        if any("404" in r or "均失败" in r for r in results):
            pytest.skip("SHFE 当日无行情（非交易日），跳过实时抓取断言")
        assert any("退出码 0" in r and "涨幅" in r for r in results), results
        # run_script 通过子进程落盘，必须广播 produced_file 事件供完成界面列出
        produced = [e for e in events if e["type"] == "produced_file"]
        assert produced, "缺少 produced_file 事件"
        assert produced[0]["data"]["path"] == "futures.md"
        # futures.md 必须落在「当前任务」沙箱内（而非全局沙箱）
        md = Path(_SANDBOX) / "tasks" / task_id / "futures.md"
        assert md.exists(), list(Path(_SANDBOX) / "tasks".glob(f"{task_id}/*"))
        assert "涨幅" in md.read_text(encoding="utf-8")
        assert runner.get(task_id)["status"] == "done"

    def test_late_subscriber_gets_full_history(self) -> None:
        script = [
            plan_json(("单步", "直接完成")),
            answer("done"),
            verdict_json(True, 88),
            answer("# deliverable"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h, parallel=1, history_path=str(_SHARED_HISTORY), codegen=False)
        task_id = runner.start("简单任务")
        drain(task_id, runner)
        events = drain(task_id, runner)  # subscribe after finish
        assert events[-1]["type"] == "task_end"
        assert events[0]["type"] == "task_start"

    def test_fast_path_skips_llm_entirely(self) -> None:
        """Deterministic objective short-circuits: the FakeRouter is never
        called — pure code answers "计算 2+3" in milliseconds."""
        h = make_harness([])  # empty script: any LLM call would yield an error event
        runner = TaskRunner(h, parallel=1, history_path=str(_SHARED_HISTORY), codegen=False)
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
        runner = TaskRunner(h, parallel=1, history_path=str(_SHARED_HISTORY), max_steps=3, codegen=False)
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
        return TestClient(create_app(harness=h, runner=TaskRunner(h, parallel=1, history_path=str(_SHARED_HISTORY), max_steps=4, codegen=False)))

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


class TestParallelTasks:
    """Subtask fan-out: with parallel>1 the batch is announced up front and
    workers execute concurrently; results come back ordered by index."""

    def test_parallel_batch_announced_then_completed(self) -> None:
        script = [
            plan_json(("A", "生成报告 A"), ("B", "生成报告 B")),
            answer("结果: 报告 A"),
            answer("结果: 报告 B"),
            verdict_json(True, 92),
            answer("# 交付\n两份报告完成"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h, parallel=2, history_path=str(_SHARED_HISTORY), codegen=False)
        task_id = runner.start("生成两份报告")
        events = drain(task_id, runner)
        types = types_of(events)

        assert types[-1] == "task_end"
        starts = [i for i, t in enumerate(types) if t == "subtask_start"]
        dones = [i for i, t in enumerate(types) if t == "subtask_done"]
        assert len(starts) == 2 and len(dones) == 2
        # fan-out: every subtask_start is announced before any subtask_done
        assert max(starts) < min(dones)
        # both workers completed and tagged their events with the right index
        done_subtasks = {e["data"]["index"] for e in events if e["type"] == "subtask_done"}
        assert done_subtasks == {0, 1}
        end = events[-1]
        assert end["data"]["passed"] is True
        assert runner.get(task_id)["status"] == "done"

    def test_parallel_single_subtask_falls_back_to_serial(self) -> None:
        """One subtask: no pool needed; still completes normally."""
        script = [
            plan_json(("A", "做一件事")),
            answer("完成"),
            verdict_json(True, 90),
            answer("# 交付\n完成"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h, parallel=2, history_path=str(_SHARED_HISTORY), codegen=False)
        task_id = runner.start("做一件事")
        events = drain(task_id, runner)
        assert types_of(events)[-1] == "task_end"


class TestHistoryPersistence:
    """Successful tasks persist their full event log; a fresh runner (restart)
    can list/read them back. Failed tasks are not persisted."""

    def test_successful_task_persisted_and_reloadable(self, tmp_path) -> None:
        hist = tmp_path / "hist.db"
        script = [
            plan_json(("A", "做一件事")),
            answer("完成"),
            verdict_json(True, 90),
            answer("# 交付\n完成"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h, parallel=1, codegen=False, history_path=str(hist))
        task_id = runner.start("做一件事")
        events = drain(task_id, runner)
        assert types_of(events)[-1] == "task_end"

        # the SQLite DB now holds the full event stream, every step
        assert hist.exists()
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(hist))
        row = conn.execute(
            "SELECT status, events FROM task_history WHERE task_id = ?", (task_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row[0] == "done"
        assert json.loads(row[1]) == events

        # a brand-new runner (simulating a restart) can read it back
        h2 = make_harness([])
        runner2 = TaskRunner(h2, parallel=1, codegen=False, history_path=str(hist))
        got = runner2.get(task_id)
        assert got is not None and got["status"] == "done"
        assert got["events"] == events
        assert task_id in {t["task_id"] for t in runner2.list()}

    def test_failed_task_not_persisted(self, tmp_path) -> None:
        hist = tmp_path / "hist.db"
        h = make_harness([])  # empty script: planner gets "(out of script)" -> error
        runner = TaskRunner(h, parallel=1, codegen=False, history_path=str(hist))
        task_id = runner.start("会失败的任务")
        events = drain(task_id, runner)
        assert types_of(events)[-1] == "error"
        # a fresh runner must not see the failed task
        h2 = make_harness([])
        runner2 = TaskRunner(h2, parallel=1, codegen=False, history_path=str(hist))
        assert runner2.get(task_id) is None


class TestTaskSandboxIsolation:
    """Each task runs in its own sandbox dir (<global>/tasks/<task_id>), so
    parallel subtasks / successive tasks never see each other's files."""

    def test_each_task_gets_isolated_directory(self, tmp_path) -> None:
        sb = tmp_path / "sb"
        script = [
            plan_json(("写文件", "写 result.txt 内容为 hello")),
            tool_call("write_file", {"path": "result.txt", "content": "hello"}),
            answer("已写入 result.txt"),
            verdict_json(True, 95),
            answer("# 交付\n完成"),
        ]
        h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(sb))
        h.router = FakeRouter(list(script))

        runner = TaskRunner(h, parallel=1, codegen=False, history_path=str(tmp_path / "h1.db"))
        tid1 = runner.start("任务一")
        events = drain(tid1, runner)
        assert types_of(events)[-1] == "task_end"

        # task 1's file lives under tasks/<tid1>/ and nowhere else at root
        assert (sb / "tasks" / tid1 / "result.txt").read_text(encoding="utf-8") == "hello"
        assert not (sb / "result.txt").exists()

        # a second task with the same relative path is fully isolated
        h.router = FakeRouter(list(script))
        runner2 = TaskRunner(h, parallel=1, codegen=False, history_path=str(tmp_path / "h2.db"))
        tid2 = runner2.start("任务二")
        events = drain(tid2, runner2)
        assert types_of(events)[-1] == "task_end"

        assert (sb / "tasks" / tid2 / "result.txt").read_text(encoding="utf-8") == "hello"
        # both survive independently — no overwrite across tasks
        assert (sb / "tasks" / tid1 / "result.txt").read_text(encoding="utf-8") == "hello"

    def test_specialist_registry_has_no_global_fs_tools(self, tmp_path) -> None:
        """The per-task registry replaces read/write/list with task-bound ones;
        verify the workspace registry binds the right directory."""
        sb = tmp_path / "sb"
        h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(sb))
        h.router = FakeRouter([])
        runner = TaskRunner(h, parallel=1, codegen=False, history_path=str(tmp_path / "h.db"))
        task_dir, registry = runner._make_task_workspace("abc123")
        assert task_dir == sb / "tasks" / "abc123"
        names = registry.names()
        assert {"read_file", "write_file", "list_files"} <= set(names)
        assert "fetch" in names and "get_current_time" in names
        # tasks may READ memory (recall) but never WRITE (remember) or list
        assert "recall" in names
        assert not {"remember", "list_memories"} & set(names)
        # writes go into the per-task dir
        registry.execute("write_file", {"path": "x.txt", "content": "hi"})
        assert (sb / "tasks" / "abc123" / "x.txt").read_text(encoding="utf-8") == "hi"
        assert not (sb / "x.txt").exists()


class UsageRouter(FakeRouter):
    """FakeRouter that also accumulates usage like LiteLLMRouter."""

    def __init__(self, script):
        super().__init__(script)
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete(self, messages, tools=None, **kwargs):
        resp = super().complete(messages, tools=tools, **kwargs)
        self.total_usage["prompt_tokens"] += 10
        self.total_usage["completion_tokens"] += 20
        self.total_usage["total_tokens"] += 30
        return resp


class TestUsageTracking:
    def test_task_end_carries_token_usage(self, tmp_path) -> None:
        script = [
            plan_json(("A", "做一件事")),
            answer("完成"),
            verdict_json(True, 90),
            answer("# 交付\n完成"),
        ]
        h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(tmp_path / "sb"))
        h.router = UsageRouter(list(script))
        runner = TaskRunner(h, parallel=1, codegen=False, history_path=str(tmp_path / "h.db"))
        task_id = runner.start("做一件事")
        events = drain(task_id, runner)

        end = next(e for e in events if e["type"] == "task_end")
        usage = end["data"]["usage"]
        # planner(1) + specialist(1) + evaluator(1) + reporter(1) = 4 calls
        assert usage == {"prompt_tokens": 40, "completion_tokens": 80, "total_tokens": 120}

    def test_fast_result_without_llm_has_zero_usage(self, tmp_path) -> None:
        """Fast path resolves without any LLM call -> usage diff is 0/absent-safe."""
        h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(tmp_path / "sb"))
        h.router = UsageRouter([])
        runner = TaskRunner(h, parallel=1, codegen=False, history_path=str(tmp_path / "h.db"))
        task_id = runner.start("计算 2+3")  # fast path: 2+3 resolves by code
        events = drain(task_id, runner)
        assert types_of(events)[-1] == "task_end"
        end = next(e for e in events if e["type"] == "task_end")
        assert end["data"]["usage"] == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }


class TestSubtasksFastPath:
    """A deterministic subtask instruction ("计算 2+3") resolves by code with
    ZERO model calls — the specialist loop is skipped entirely."""

    def test_deterministic_subtask_uses_no_llm(self, tmp_path) -> None:
        script = [
            plan_json(("计算", "计算 2+3"), ("报告", "写一句话报告")),
            answer("完成报告"),
            verdict_json(True, 90),
            answer("# 交付\n完成"),
        ]
        h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(tmp_path / "sb"))
        h.router = UsageRouter(list(script))
        runner = TaskRunner(h, parallel=1, codegen=False, history_path=str(tmp_path / "h.db"))
        task_id = runner.start("做两件事")
        events = drain(task_id, runner)

        # LLM calls: planner(1) + specialist(normal)(1) + evaluator(1) + reporter(1)
        # — the arithmetic subtask consumed none
        end = next(e for e in events if e["type"] == "task_end")
        assert end["data"]["usage"] == {
            "prompt_tokens": 40,
            "completion_tokens": 80,
            "total_tokens": 120,
        }
        # the arithmetic subtask finished via fast path, flagged in the event
        dones = [e["data"] for e in events if e["type"] == "subtask_done"]
        fast = next(d for d in dones if d.get("fast"))
        assert "计算" in fast["title"]
        assert "5" in fast["summary"] or "2+3" in fast["summary"]
        # non-fast subtask has no flag
        normal = next(d for d in dones if not d.get("fast"))
        assert "报告" in normal["title"]
        assert types_of(events)[-1] == "task_end"

    def test_subtask_fastpath_reads_per_task_sandbox(self, tmp_path) -> None:
        """子任务的 fast-path「读取沙箱里的 X」必须读 per-task 隔离目录，
        而非全局沙箱根（修复前会泄漏/误读全局文件）。"""
        sb = tmp_path / "sb"
        sb.mkdir(parents=True, exist_ok=True)
        # 全局沙箱放一个同名文件，内容不同 —— 用来区分读的是哪层目录
        (sb / "note.txt").write_text("GLOBAL_CONTENT", encoding="utf-8")

        script = [
            plan_json(("写文件", "写 note.txt"), ("读取", "读取沙箱里的 note.txt")),
            tool_call("write_file", {"path": "note.txt", "content": "TASK_CONTENT"}),
            answer("已写入 note.txt"),
            verdict_json(True, 90),
            answer("# 交付\n完成"),
        ]
        h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(sb))
        h.router = UsageRouter(list(script))
        runner = TaskRunner(h, parallel=1, codegen=False, history_path=str(tmp_path / "h.db"))
        task_id = runner.start("写再读")
        events = drain(task_id, runner)
        assert types_of(events)[-1] == "task_end"

        dones = [e["data"] for e in events if e["type"] == "subtask_done"]
        read = next(d for d in dones if d.get("fast") and "读取" in d["title"])
        # 读到的是 per-task 隔离目录里的 TASK_CONTENT，而不是全局的 GLOBAL_CONTENT
        assert "TASK_CONTENT" in read["summary"]
        assert "GLOBAL_CONTENT" not in read["summary"]
        # 全局文件未被触碰
        assert (sb / "note.txt").read_text(encoding="utf-8") == "GLOBAL_CONTENT"


class TestStop:
    """后端必须能真正中止运行中的任务，而不能只断开前端 SSE。"""

    def test_run_aborts_at_checkpoint_when_stop_requested(self) -> None:
        """stop_requested 置位后，_run 在安全点立即中止并发出 task_stopped。"""
        h = make_harness([plan_json(("a", "b"))])
        runner = TaskRunner(h, history_path=_SHARED_HISTORY, codegen=False)
        task_id = "stop-unit"
        record = TaskRecord(task_id, "非算术目标", "fake-model")
        runner._records[task_id] = record
        record.stop_requested = True
        # 同步跑 _run（不经过 start 的线程），确定性验证检查点
        runner._run(task_id)
        assert record.status == "stopped"
        assert "task_stopped" in types_of(record.events)

    def test_stop_method_returns_bool(self) -> None:
        h = make_harness([plan_json(("a", "b"))])
        runner = TaskRunner(h, history_path=_SHARED_HISTORY, codegen=False)
        assert runner.stop("unknown") is False
        rec = TaskRecord("r1", "g", "m")
        runner._records["r1"] = rec
        assert runner.stop("r1") is True
        assert rec.stop_requested is True
        # 已停止的任务无法再次停止
        rec.status = "stopped"
        assert runner.stop("r1") is False

    def test_stop_endpoint(self) -> None:
        client = TestClient(
            create_app(
                harness=make_harness([plan_json(("a", "b"))]),
                runner=TaskRunner(
                    make_harness([]), parallel=1, history_path=_SHARED_HISTORY, codegen=False
                ),
            )
        )
        runner = client.app.state.runner
        rec = TaskRecord("s1", "g", "m")
        runner._records["s1"] = rec
        r = client.post("/api/tasks/s1/stop")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 未知任务 -> ok False
        assert client.post("/api/tasks/nope/stop").json()["ok"] is False

    def test_execute_plan_stops_between_subtasks(self) -> None:
        """串行执行中途被停止：检查点（子任务之间）生效，发出 task_stopped。"""
        script = [
            plan_json(("甲", "做算术报告"), ("乙", "写一句总结")),
            tool_call("add", {"a": 1, "b": 1}),
            answer("结果 2"),
            verdict_json(True, 90),
            answer("# 交付"),
        ]
        h = make_harness(script)
        runner = TaskRunner(h, parallel=1, history_path=_SHARED_HISTORY, codegen=False)
        task_id = "stop-partial"
        rec = TaskRecord(task_id, "目标", "fake-model")
        runner._records[task_id] = rec
        # 进入 replan 循环前即置位停止：_run 应在检查点中止，不执行任何子任务
        rec.stop_requested = True
        runner._run(task_id)
        assert rec.status == "stopped"
        assert "task_stopped" in types_of(rec.events)
