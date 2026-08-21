"""Multi-agent task orchestration.

Given an objective, the runner choreographs three roles:

    Planner    breaks the objective into structured subtasks (JSON)
    Specialist executes each subtask as its own tool loop (reuses build_loop)
    Evaluator  verifies the result; on failure the plan is re-done once,
               then a Reporter composes the final deliverable.

Every inner step is emitted as an event (SSE-friendly), tagged with the
subtask it belongs to so a frontend can render a task tree:

    task_start -> plan -> [subtask_start -> thought/tool_call/tool_result
                            -> subtask_done] *N
                -> evaluation -> (re_plan -> plan -> ... | task_end)
                -> error

Threading: start() spawns a daemon thread; subscribe() returns a queue.Queue
the SSE handler drains; a None sentinel closes the stream.
"""

from __future__ import annotations

import datetime
import json
import queue
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .graph.loop import build_loop
from .harness import Harness, memory_hint
from .llm import LiteLLMRouter, usage_diff, usage_snapshot
from .roles import Evaluator, Planner, _lang_name
from .tools.fs import make_fs_tools
from .tools.registry import ToolRegistry

MAX_REPLAN_ROUNDS = 1  # hard ceiling on plan-revision rounds (anti-runaway)

# How many successful tasks the persisted history keeps (oldest dropped).
HISTORY_KEEP = 500


def default_history_path() -> Path:
    """Persisted task log: <project root>/data/task_history.db (SQLite)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent / "data" / "task_history.db"
    return Path.cwd() / "data" / "task_history.db"


def default_sandbox_dir() -> Path:
    """Global sandbox root: <project root>/data/sandbox."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent / "data" / "sandbox"
    return Path.cwd() / "data" / "sandbox"

SPECIALIST_PROMPT = """You are a SPECIALIST executor in a multi-agent task system. Complete your assigned subtask, then report what you did.

Subtask: {title}
Instruction: {instruction}

Context:
{context}

Rules:
1. THIS SUBTASK RUNS IN ITS OWN ISOLATED SANDBOX, IN PARALLEL WITH OTHERS. Never read or assume files written by other subtasks — they are not visible to you. Get any data you need yourself (fetch/compute/write) inside this subtask.
2. Batch independent tool calls into ONE message: if you need several values at once, call the tool multiple times in a single reply.
3. TRUST tool results. Never recompute what a tool already returned.
4. Prefer the minimum number of tool calls that fully answers the subtask. One batch, then summarize.
5. Use tools when they help: write_file/read_file for artifacts (ALWAYS relative sandbox paths — never /tmp, /app or any absolute path), get_current_time for time. Arithmetic has no tool — pure math is resolved by code; compute simple values yourself. You may `recall` an existing long-term fact if needed, but NEVER write to long-term memory (no remember) — task data lives only in the sandbox files you create.
6. When done, end with a concise summary, and put every key computed value on its own line, e.g. "结果: 68".

Language: write your thinking and your final summary in {language} — never in English.

Begin."""

REPORTER_PROMPT = """You are the REPORTER of a multi-agent task system. Compose the final deliverable for the user based on the executed work.

Objective: {objective}

Subtasks executed:
{plan}

Results:
{results}

Write a final deliverable in Markdown: a short opening summary, what was done per subtask, and where artifacts live. Be concrete and complete. Output only the deliverable.

Language: write the entire deliverable in {language} — never in English."""


class TaskRecord:
    """Mutable state for one running/finished task."""

    def __init__(self, task_id: str, objective: str, model: str) -> None:
        self.task_id = task_id
        self.objective = objective
        self.model = model
        self.status: str = "running"  # running | done | error
        self.events: list[dict[str, Any]] = []
        self.created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        self.error: str | None = None
        self.queue: queue.Queue = queue.Queue()

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at,
            "error": self.error,
            "events": self.events,
        }


class TaskRunner:
    """Orchestrates Planner -> Specialists -> Evaluator -> Reporter."""

    def __init__(
        self,
        harness: Harness,
        *,
        language: str = "zh",
        max_steps: int | None = None,
        max_replan_rounds: int = MAX_REPLAN_ROUNDS,
        plugin_dir: str | None = None,
        codegen: bool = True,
        parallel: int = 4,
        history_path: str | Path | None = None,
        agent_models: dict[str, str] | None = None,
    ) -> None:
        self.harness = harness
        self.router: LiteLLMRouter = harness.router
        self.language = language
        self.max_steps = max_steps or harness.settings.max_steps
        self.max_replan_rounds = max_replan_rounds
        self.plugin_dir = plugin_dir  # None -> data/fastpath_plugins
        self.codegen = codegen  # allow tests to pin the codegen step off
        # per-agent LLM override: {"planner"|"specialist"|"evaluator"|"reporter": model}.
        # absent/empty keys fall back to the router default model.
        self.agent_models = {k: v for k, v in (agent_models or {}).items() if v}
        self._planner = Planner(
            self.router,
            language=language,
            model=self.agent_models.get("planner"),
        )
        self._evaluator = Evaluator(
            self.router,
            language=language,
            model=self.agent_models.get("evaluator"),
        )
        # parallel>1 runs the plan's subtasks concurrently (fan-out). The
        # router / memory / registry are thread-safe; results are collected
        # by index so the reporter still sees a stable order.
        self.parallel = max(1, parallel)
        # Successful tasks are persisted (full event stream) to a SQLite DB so
        # the operation log survives restarts; None -> data/task_history.db.
        # Same pattern as LongTermMemory: stdlib sqlite3 + lock, thread-safe.
        self.history_path = str(history_path) if history_path else str(default_history_path())
        self._conn: sqlite3.Connection | None = None
        self._db_lock = threading.RLock()
        self._history: dict[str, dict[str, Any]] = self._load_history()
        self._records: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

    def set_agent_models(self, models: dict[str, str]) -> None:
        """Runtime update of per-agent LLM overrides; applies to the next task.

        Planner/Evaluator instances are constructed once, so their model is
        refreshed here; Specialist/Reporter read `agent_models` per task.
        Unknown keys are dropped.
        """
        self.agent_models = {
            k: v for k, v in models.items() if k in ("planner", "specialist", "evaluator", "reporter") and v
        }
        self._planner.model = self.agent_models.get("planner")
        self._evaluator.model = self.agent_models.get("evaluator")

    # -- history (persisted task log, SQLite) --------------------------------------

    def _db(self) -> sqlite3.Connection:
        """Lazily open the history DB and ensure the schema exists."""
        if self._conn is None:
            path = Path(self.history_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_history (
                    task_id    TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    model     TEXT NOT NULL,
                    status    TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    error     TEXT,
                    events    TEXT NOT NULL
                )
                """
            )
            self._conn.commit()
        return self._conn

    def _load_history(self) -> dict[str, dict[str, Any]]:
        """Load previously persisted successful tasks into memory (newest last
        in the dict is irrelevant — list() sorts by created_at)."""
        out: dict[str, dict[str, Any]] = {}
        try:
            with self._db_lock:
                rows = self._db().execute(
                    "SELECT task_id, objective, model, status, created_at, error, events"
                    " FROM task_history"
                ).fetchall()
            for task_id, objective, model, status, created_at, error, events in rows:
                try:
                    evts = json.loads(events) if events else []
                except json.JSONDecodeError:
                    evts = []
                out[task_id] = {
                    "task_id": task_id,
                    "objective": objective,
                    "model": model,
                    "status": status,
                    "created_at": created_at,
                    "error": error,
                    "events": evts,
                }
        except sqlite3.Error:
            pass
        return out

    def _persist_task(self, record: TaskRecord) -> None:
        """Upsert a successful task's full event log; bound the table size."""
        try:
            snap = record.snapshot()
            with self._db_lock:
                conn = self._db()
                conn.execute(
                    "INSERT OR REPLACE INTO task_history"
                    " (task_id, objective, model, status, created_at, error, events)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        snap["task_id"],
                        snap["objective"],
                        snap["model"],
                        snap["status"],
                        snap["created_at"],
                        snap["error"],
                        json.dumps(snap["events"], ensure_ascii=False),
                    ),
                )
                conn.execute(
                    "DELETE FROM task_history WHERE task_id IN ("
                    " SELECT task_id FROM task_history"
                    " ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                    (HISTORY_KEEP,),
                )
                conn.commit()
            self._history[record.task_id] = snap
        except sqlite3.Error:
            pass  # persistence is best-effort; the in-memory record still works

    # -- public API -----------------------------------------------------------

    def start(self, objective: str) -> str:
        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(task_id, objective, self.harness.settings.model)
        with self._lock:
            self._records[task_id] = record
        thread = threading.Thread(target=self._run, args=(task_id,), daemon=True)
        thread.start()
        return task_id

    def get(self, task_id: str) -> dict[str, Any] | None:
        record = self._records.get(task_id)
        if record:
            return record.snapshot()
        return self._history.get(task_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records.values())
        snaps = [r.snapshot() for r in records]
        for hid, hsnap in self._history.items():
            if hid not in self._records:
                snaps.append(hsnap)
        # newest first (in-memory and persisted logs share one timeline)
        snaps.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        return snaps

    def subscribe(self, task_id: str) -> queue.Queue | None:
        record = self._records.get(task_id)
        if record is None:
            return None
        if record.status != "running":
            q: queue.Queue = queue.Queue()
            for ev in record.events:
                q.put(ev)
            q.put(None)
            return q
        return record.queue

    # -- event plumbing ----------------------------------------------------------

    def _emit(self, record: TaskRecord, event_type: str, data: dict[str, Any]) -> None:
        event: dict[str, Any] = {
            "task_id": record.task_id,
            "type": event_type,
            "data": data,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
        }
        record.events.append(event)
        record.queue.put(event)

    # -- orchestration -------------------------------------------------------------

    def _run(self, task_id: str) -> None:
        record = self._records[task_id]
        try:
            self._emit(record, "task_start", {"objective": record.objective, "model": record.model})
            objective = record.objective
            # token accounting for THIS task (planner + specialists + evaluator + reporter)
            usage_baseline = usage_snapshot(self.router)

            # Fast path: deterministic queries (arithmetic / time / conversions…)
            # are resolved by code — no Planner/Specialist/Evaluator LLM at all.
            from .fastpath import try_fast_answer

            fast = try_fast_answer(
                objective,
                sandbox_dir=self.harness.sandbox_dir,
                plugin_dir=self.plugin_dir,
            )
            if fast is not None:
                self._emit_fast_result(
                    record,
                    objective,
                    fast.method,
                    fast.answer,
                    fast.detail,
                    usage=usage_diff(self.router, usage_baseline),
                )
                return

            # Codegen: ask the LLM once whether this can be solved by a generated
            # detector; on success the detector is persisted and reused forever.
            if self.codegen:
                from .codegen import codegen_solve

                gen_answer = codegen_solve(self.router, objective, plugin_dir=self.plugin_dir)
                if gen_answer is not None:
                    self._emit_fast_result(
                        record,
                        objective,
                        "codegen",
                        gen_answer,
                        gen_answer[:200],
                        usage=usage_diff(self.router, usage_baseline),
                    )
                    return

            # One isolated sandbox + tool registry per task (parallel-safe).
            task_sandbox, task_registry = self._make_task_workspace(task_id)

            for round_no in range(self.max_replan_rounds + 1):
                plan = self._planner.plan(objective)
                self._emit(record, "plan", {"objective": objective, "subtasks": plan, "round": round_no})

                results = self._execute_plan(record, plan, registry=task_registry)

                verdict = self._evaluator.evaluate(
                    objective,
                    plan,
                    results,
                    deliverable=_summarize_results(results),
                )
                self._emit(record, "evaluation", {**verdict, "round": round_no})

                if verdict["passed"] or round_no >= self.max_replan_rounds:
                    deliverable = self._compose_deliverable(objective, plan, results)
                    self._emit(
                        record,
                        "task_end",
                        {
                            "reply": deliverable,
                            "passed": bool(verdict["passed"]),
                            "score": int(verdict.get("score", 0)),
                            "rounds": round_no + 1,
                            "usage": usage_diff(self.router, usage_baseline),
                        },
                    )
                    record.status = "done"
                    # Successful task: persist every step so the operation log
                    # survives a restart (best-effort; never raises).
                    self._persist_task(record)
                    return

                # not passed and rounds remain -> re-plan with feedback
                feedback = str(verdict.get("feedback", "objective not met"))
                self._emit(record, "re_plan", {"feedback": feedback, "round": round_no})
                objective = _replan_objective(objective, plan, results, feedback)

            raise RuntimeError("unreachable: replan loop bounded")
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            record.error = str(exc)
            record.status = "error"
            self._emit(
                record,
                "error",
                {"message": str(exc), "usage": usage_diff(self.router, usage_baseline)},
            )
        finally:
            record.queue.put(None)

    # -- per-task isolated workspace ----------------------------------------------

    def _make_task_workspace(self, task_id: str) -> tuple[Path, ToolRegistry]:
        """Give every task its own sandbox directory + registry so parallel
        subtasks never see each other's (or older tasks') files.

        The fs tools are rebuilt bound to `<global_sandbox>/tasks/<task_id>/`;
        every other tool (fetch / get_current_time…) is copied as-is from the
        harness registry (memory tools excluded on purpose).
        """
        base = Path(self.harness.sandbox_dir) if self.harness.sandbox_dir else default_sandbox_dir()
        task_dir = base / "tasks" / task_id
        registry = ToolRegistry()
        for tool in self.harness.tools:
            if tool.name in {"read_file", "write_file", "list_files"}:
                continue  # rebuilt below with the per-task sandbox
            if tool.name in {"remember", "list_memories"}:
                # Tasks never WRITE long-term memory (specialists storing
                # intermediate artifacts pollutes cross-session facts). recall
                # is allowed: a specialist may read an existing snapshot.
                continue
            registry.register(
                tool.func,
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
                require_approval=tool.require_approval,
            )
        for fn in make_fs_tools(task_dir):
            registry.register(fn)
        return task_dir, registry

    # -- specialists ------------------------------------------------------------------

    def _execute_plan(
        self,
        record: TaskRecord,
        plan: list[dict[str, Any]],
        registry: ToolRegistry,
    ) -> list[dict[str, Any]]:
        total = len(plan)
        # Announce every subtask up front: with parallel workers the UI shows
        # the whole batch as running cards at once instead of one-by-one.
        for i, st in enumerate(plan):
            self._emit(
                record,
                "subtask_start",
                {"index": i, "title": st["title"], "total": total},
            )

        # Serial mode keeps the "earlier results as context" semantics; the
        # lock guards the shared list between the main thread and workers.
        done: list[dict[str, Any]] = []
        done_lock = threading.Lock()

        def run_one(i: int, st: dict[str, Any]) -> dict[str, Any]:
            # Subtask-level Fast Path: a deterministic instruction ("计算 2+3",
            # "现在几点"…) is resolved by code with ZERO model calls. The
            # specialist loop is skipped entirely.
            from .fastpath import try_fast_answer

            fast = try_fast_answer(
                st["instruction"],
                sandbox_dir=self.harness.sandbox_dir,
                plugin_dir=self.plugin_dir,
            )
            if fast is not None:
                def emit_fast(kind: str, data: dict[str, Any]) -> None:
                    self._emit(record, kind, {**data, "subtask": i})

                emit_fast("tool_call", {"name": fast.method, "args": {}})
                emit_fast("tool_result", {"name": fast.method, "content": fast.detail})
                summary = fast.answer
                result = {"index": i, "title": st["title"], "summary": summary}
                if self.parallel <= 1:
                    with done_lock:
                        done.append(result)
                self._emit(
                    record,
                    "subtask_done",
                    {"index": i, "title": st["title"], "summary": summary, "fast": True},
                )
                return result

            if self.parallel > 1:
                # Parallel mode: there is no "earlier result" — every worker
                # starts at once, so give each one the full plan picture.
                context = _parallel_context(plan)
            else:
                with done_lock:
                    context = _format_context(list(done))
            system_prompt = SPECIALIST_PROMPT.format(
                title=st["title"],
                instruction=st["instruction"],
                context=context,
                language=_lang_name(self.language),
            )
            # Memory pre-fetch: relevant long-term snapshots (e.g. a stored
            # quote) are injected so the specialist can use them instead of
            # re-fetching. Read-only — task mode still can't write memory.
            hint = memory_hint(self.harness.long_term, self.harness.memory_scope, st["instruction"])
            if hint:
                system_prompt = f"{system_prompt}\n\n{hint}"

            def emit_for_subtask(kind: str, data: dict[str, Any], _idx: int = i) -> None:
                self._emit(record, kind, {**data, "subtask": _idx})

            loop = build_loop(
                self.router,
                registry,
                system_prompt=system_prompt,
                verbose=self.harness.settings.verbose,
                emit=emit_for_subtask,
                model=self.agent_models.get("specialist"),
            )
            final = loop.invoke(
                {
                    "messages": [{"role": "user", "content": st["instruction"]}],
                    "step": 0,
                    "max_steps": self.max_steps,
                }
            )
            summary = self._final_reply(final.get("messages", [])) or "(no summary)"
            result = {"index": i, "title": st["title"], "summary": summary}
            if self.parallel <= 1:
                with done_lock:
                    done.append(result)
            self._emit(record, "subtask_done", {"index": i, "title": st["title"], "summary": summary})
            return result

        if self.parallel > 1 and total > 1:
            with ThreadPoolExecutor(max_workers=min(self.parallel, total)) as pool:
                futures = [pool.submit(run_one, i, st) for i, st in enumerate(plan)]
                results: list[dict[str, Any]] = []
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception:
                        # One failing subtask fails the task (same as serial);
                        # cancel what hasn't started yet, then re-raise.
                        for other in futures:
                            other.cancel()
                        raise
            results.sort(key=lambda r: r["index"])
            return results

        return [run_one(i, st) for i, st in enumerate(plan)]

    def _emit_fast_result(
        self,
        record: TaskRecord,
        objective: str,
        method: str,
        answer: str,
        detail: str,
        usage: dict[str, int] | None = None,
    ) -> None:
        """Emit a complete task tree for a code-resolved answer (no LLM loop)."""
        label = {
            "codegen": "代码生成解决（检测器已持久化，下次零模型）",
            "plugin": "插件命中（此前生成的检测器）",
        }.get(method, "代码直接计算（无需模型）")
        self._emit(
            record,
            "plan",
            {
                "objective": objective,
                "subtasks": [{"index": 0, "title": label, "instruction": objective, "artifacts": []}],
                "round": 0,
            },
        )
        self._emit(record, "subtask_start", {"index": 0, "title": label, "total": 1})
        self._emit(
            record,
            "tool_result",
            {"name": method, "content": detail, "step": 1, "subtask": 0},
        )
        self._emit(record, "subtask_done", {"index": 0, "title": label, "summary": answer})
        self._emit(
            record,
            "evaluation",
            {"passed": True, "score": 100, "feedback": "由代码直接完成，零模型调用", "missing": []},
        )
        self._emit(
            record,
            "task_end",
            {"reply": answer, "passed": True, "score": 100, "rounds": 1, "usage": usage},
        )
        record.status = "done"
        self._persist_task(record)

    def _compose_deliverable(
        self,
        objective: str,
        plan: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> str:
        try:
            response = self.router.complete(
                [
                    {
                        "role": "user",
                        "content": REPORTER_PROMPT.format(
                            objective=objective,
                            plan=_format_plan_for_reporter(plan),
                            results=_format_results_for_reporter(results),
                            language=_lang_name(self.language),
                        ),
                    }
                ],
                temperature=0.3,
                model=self.agent_models.get("reporter"),
            )
            return (response.get("content") or "").strip() or _summarize_results(results)
        except Exception:  # noqa: BLE001 - fall back to plain summaries
            return _summarize_results(results)

    @staticmethod
    def _final_reply(messages: list[Any]) -> str:
        for m in reversed(messages):
            if isinstance(m, dict):
                if m.get("role") == "assistant" and m.get("content"):
                    return str(m["content"])
            elif getattr(m, "type", "") == "ai" and m.content:
                return str(m.content)
        return ""


# -- helpers ----------------------------------------------------------------------


def _format_context(results: list[dict[str, Any]]) -> str:
    if not results:
        return "(none — this is the first subtask)"
    return "\n".join(f"[{r['index']}] {r['title']}: {r.get('summary', '')[:1000]}" for r in results)


def _parallel_context(plan: list[dict[str, Any]]) -> str:
    """Parallel-mode context: no earlier results exist, but every worker gets
    the full plan so it understands where its subtask fits."""
    titles = "、".join(f"[{s['index']}] {s['title']}" for s in plan)
    return f"（并行执行，无前序结果；本任务共 {len(plan)} 个子任务：{titles}）"


def _replan_objective(
    objective: str,
    plan: list[dict[str, Any]],
    results: list[dict[str, Any]],
    feedback: str,
) -> str:
    """New objective for the planner that carries evaluator feedback + prior work."""
    return (
        f"{objective}\n\n"
        f"[Evaluator feedback from the previous round]\n{feedback}\n"
        f"[Work already done]\n{_format_context(results)}\n"
        "Revise the plan to fix the issues; do not repeat completed work blindly."
    )


def _summarize_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "(no subtask results)"
    return "\n".join(f"- {r['title']}: {r.get('summary', '')[:1200]}" for r in results)


def _format_plan_for_reporter(plan: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{st['index']}] {st['title']}" for st in plan)


def _format_results_for_reporter(results: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {r['title']}: {r.get('summary', '')}" for r in results)
