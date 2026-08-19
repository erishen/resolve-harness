"""Tests for runtime code generation: sandboxed execution, persistence,
and end-to-end "generate once, reuse forever" in the task runner."""

from __future__ import annotations

import queue
import time
from typing import Any

import pytest

from agentpulse.codegen import (
    CodeGenError,
    codegen_solve,
    extract_code,
    load_plugins,
    run_detector,
    save_plugin,
)
from agentpulse.fastpath import try_fast_answer
from agentpulse.harness import Harness
from agentpulse.tasks import TaskRunner


class TestSandboxExecution:
    def test_simple_detector(self) -> None:
        source = '''
def detect(text):
    if "倒过来" in text and "「" in text:
        m = text.split("「")[1].split("」")[0]
        return m + " 倒过来是 " + m[::-1] + "。"
    return None
'''
        assert run_detector(source, "「hello」倒过来是什么") == "hello 倒过来是 olleh。"
        assert run_detector(source, "写一篇报告") is None

    def test_string_methods_allowed(self) -> None:
        source = '''
def detect(text):
    if "大写" in text:
        for part in text.split("「")[1:]:
            return part.split("」")[0].upper()
    return None
'''
        assert run_detector(source, "把「abc」转大写") == "ABC"

    @pytest.mark.parametrize(
        "evil_source",
        [
            "import os\ndef detect(t):\n    return os.getcwd()",
            "def detect(t):\n    return eval(t)",
            "def detect(t):\n    return open('/etc/passwd').read()",
            "def detect(t):\n    return t.__class__.__mro__",
            "def detect(t):\n    return getattr(t, 'upper')",
            "def detect(t):\n    while True:\n        pass",
            "class Evil:\n    pass\ndef detect(t):\n    return 'x'",
            "def detect(t):\n    return globals()",
        ],
    )
    def test_malicious_code_rejected(self, evil_source: str) -> None:
        assert run_detector(evil_source, "anything") is None

    def test_broken_detector_no_crash(self) -> None:
        assert run_detector("def detect(t):\n    raise ValueError('boom')", "x") is None
        assert run_detector("not python at all {{{", "x") is None
        assert run_detector("x = 1", "x") is None  # no detect function

    def test_timeout_guard(self) -> None:
        source = "def detect(t):\n    return str(sum(i for i in range(10**9)))"
        start = time.monotonic()
        assert run_detector(source, "x", timeout=0.5) is None
        assert time.monotonic() - start < 5  # hard-capped, not hung


class TestExtractCode:
    def test_fenced_python(self) -> None:
        out = extract_code("```python\ndef detect(t):\n    return 'x'\n```")
        assert out is not None and "def detect" in out

    def test_bare_function(self) -> None:
        out = extract_code("def detect(t):\n    return 'x'")
        assert out is not None

    def test_none_declined(self) -> None:
        assert extract_code("NONE") is None
        assert extract_code("无法确定性解决") is None
        assert extract_code("") is None


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path) -> None:
        source = "def detect(t):\n    if '测试' in t:\n        return 'ok'\n    return None"
        name = save_plugin(source, tmp_path)
        assert (tmp_path / f"{name}.py").exists()
        detectors = load_plugins(tmp_path)
        assert len(detectors) == 1
        assert detectors[0]("测试一下") == "ok"
        assert detectors[0]("别的") is None

    def test_save_dedupes_identical_source(self, tmp_path) -> None:
        source = "def detect(t):\n    return 'same'"
        n1 = save_plugin(source, tmp_path, trigger="第一次")
        n2 = save_plugin(source, tmp_path, trigger="第二次")
        assert n1 == n2  # hash-named: same source -> same file
        assert len(list(tmp_path.glob("gen_*.py"))) == 1
        # trigger comment from first save only (second was a no-op)
        content = (tmp_path / f"{n1}.py").read_text(encoding="utf-8")
        assert "trigger: 第一次" in content
        assert "trigger: 第二次" not in content

    def test_save_different_source_different_file(self, tmp_path) -> None:
        n1 = save_plugin("def detect(t):\n    return 'a'", tmp_path)
        n2 = save_plugin("def detect(t):\n    return 'b'", tmp_path)
        assert n1 != n2
        assert len(list(tmp_path.glob("gen_*.py"))) == 2

    def test_save_refuses_unsafe(self, tmp_path) -> None:
        with pytest.raises(CodeGenError):
            save_plugin("import os\ndef detect(t):\n    return 'x'", tmp_path)

    def test_load_skips_bad_files(self, tmp_path) -> None:
        (tmp_path / "bad.py").write_text("import os\ndef detect(t):\n    return 'x'", encoding="utf-8")
        (tmp_path / "ok.py").write_text("def detect(t):\n    return 'fine'", encoding="utf-8")
        assert [d("x") for d in load_plugins(tmp_path)] == ["fine"]

    def test_load_plugins_cached_until_change(self, tmp_path) -> None:
        from agentpulse.codegen import _load_cache

        (tmp_path / "a.py").write_text("def detect(t):\n    return 'a'", encoding="utf-8")
        first = load_plugins(tmp_path)
        key = str(tmp_path)
        assert key in _load_cache

        # no change -> same cached objects, no re-read/exec
        second = load_plugins(tmp_path)
        assert second is first

        # new plugin persisted -> reload picks it up
        (tmp_path / "b.py").write_text("def detect(t):\n    return 'b'", encoding="utf-8")
        third = load_plugins(tmp_path)
        assert len(third) == 2
        assert sorted(d("x") for d in third) == ["a", "b"]


class TestCodegenSolve:
    def test_generates_and_runs(self, tmp_path) -> None:
        class GenRouter:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, messages, **kwargs):
                self.calls += 1
                return {
                    "role": "assistant",
                    "content": 'def detect(text):\n    if "的平方" in text:\n        digits = "".join(ch for ch in text if ch.isdigit())\n        if digits:\n            n = int(digits)\n            return str(n) + " 的平方是 " + str(n * n) + "。"\n    return None',
                }

        router = GenRouter()
        answer = codegen_solve(router, "计算 7 的平方", plugin_dir=str(tmp_path))
        assert answer == "7 的平方是 49。"
        assert router.calls == 1
        # persisted: a plugin now exists and resolves the query
        assert len(list(tmp_path.glob("gen_*.py"))) == 1
        assert try_fast_answer("计算 7 的平方", plugin_dir=str(tmp_path)) is not None

    def test_declines_when_none(self, tmp_path) -> None:
        class NoneRouter:
            def complete(self, messages, **kwargs):
                return {"role": "assistant", "content": "NONE"}

        assert codegen_solve(NoneRouter(), "写一首诗", plugin_dir=str(tmp_path)) is None


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
        raise TimeoutError("task did not finish")
    return events


class TestTaskRunnerIntegration:
    GENERATED = (
        'def detect(text):\n'
        '    if "的平方" in text:\n'
        '        digits = "".join(ch for ch in text if ch.isdigit())\n'
        '        if digits:\n'
        '            n = int(digits)\n'
        '            return str(n) + " 的平方是 " + str(n * n) + "。"\n'
        '    return None'
    )

    def test_generate_once_reuse_forever(self, tmp_path) -> None:
        plugin_dir = tmp_path / "plugins"

        class GenRouter:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, messages, **kwargs):
                self.calls += 1
                return {"role": "assistant", "content": TestTaskRunnerIntegration.GENERATED}

        h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(tmp_path / "sb"))
        h.router = GenRouter()
        runner = TaskRunner(h, plugin_dir=str(plugin_dir))

        # first run: codegen generates + persists
        events = drain(runner.start("计算 7 的平方"), runner)
        assert events[-1]["type"] == "task_end"
        assert "49" in events[-1]["data"]["reply"]

        # second run: plugin hits, router NOT called
        h.router.calls = 0
        events = drain(runner.start("计算 7 的平方"), runner)
        assert "49" in events[-1]["data"]["reply"]
        assert h.router.calls == 0

    def test_codegen_decline_falls_through_to_planner(self, tmp_path) -> None:
        plugin_dir = tmp_path / "plugins"
        script = [
            {"role": "assistant", "content": "NONE"},  # 1: codegen declines
            {  # 2: planner emits a plan
                "role": "assistant",
                "content": '{"subtasks": [{"title": "写报告", "instruction": "写报告", "artifacts": []}]}',
            },
            {"role": "assistant", "content": "报告完成"},  # 3: specialist
            {  # 4: evaluator passes
                "role": "assistant",
                "content": '{"passed": true, "score": 90, "feedback": "ok", "missing": []}',
            },
            {"role": "assistant", "content": "# 报告"},  # 5: reporter
        ]

        class ScriptedRouter:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, messages, **kwargs):
                resp = script[min(self.calls, len(script) - 1)]
                self.calls += 1
                return resp

            def parse_tool_calls(self, message):
                import json as _json

                calls = []
                for tc in message.get("tool_calls") or []:
                    fn = tc["function"]
                    try:
                        args = _json.loads(fn["arguments"])
                    except Exception:
                        args = {}
                    calls.append({"id": tc["id"], "name": fn["name"], "arguments": args})
                return calls

        h = Harness(memory_db=":memory:", max_steps=6, sandbox_dir=str(tmp_path / "sb"))
        h.router = ScriptedRouter()
        runner = TaskRunner(h, plugin_dir=str(plugin_dir), max_replan_rounds=0)
        events = drain(runner.start("写一份简单报告"), runner)
        assert events[-1]["type"] == "task_end"
        assert "报告" in events[-1]["data"]["reply"]  # reporter's deliverable
        assert h.router.calls >= 4  # planner + specialist + evaluator + reporter all ran
