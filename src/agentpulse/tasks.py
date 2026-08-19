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
import queue
import threading
import uuid
from typing import Any, Callable

from .graph.loop import build_loop
from .harness import Harness
from .llm import LiteLLMRouter
from .roles import Evaluator, Planner, _lang_name

MAX_REPLAN_ROUNDS = 1  # hard ceiling on plan-revision rounds (anti-runaway)

SPECIALIST_PROMPT = """You are a SPECIALIST executor in a multi-agent task system. Complete your assigned subtask, then report what you did.

Subtask: {title}
Instruction: {instruction}

Results of earlier subtasks (context):
{context}

Rules:
1. Use tools when they help: write_file/read_file for producing artifacts, remember/recall for facts, add/get_current_time for computation and time.
2. Do NOT redo earlier subtasks; build on the context above.
3. When done, end with a concise summary of what you produced and where it is.

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
    ) -> None:
        self.harness = harness
        self.router: LiteLLMRouter = harness.router
        self.language = language
        self.max_steps = max_steps or harness.settings.max_steps
        self.max_replan_rounds = max_replan_rounds
        self._planner = Planner(self.router, language=language)
        self._evaluator = Evaluator(self.router, language=language)
        self._records: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

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
        return record.snapshot() if record else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records.values())
        return [r.snapshot() for r in records]

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

            for round_no in range(self.max_replan_rounds + 1):
                plan = self._planner.plan(objective)
                self._emit(record, "plan", {"objective": objective, "subtasks": plan, "round": round_no})

                results = self._execute_plan(record, plan)

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
                        },
                    )
                    record.status = "done"
                    return

                # not passed and rounds remain -> re-plan with feedback
                feedback = str(verdict.get("feedback", "objective not met"))
                self._emit(record, "re_plan", {"feedback": feedback, "round": round_no})
                objective = _replan_objective(objective, plan, results, feedback)

            raise RuntimeError("unreachable: replan loop bounded")
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            record.error = str(exc)
            record.status = "error"
            self._emit(record, "error", {"message": str(exc)})
        finally:
            record.queue.put(None)

    # -- specialists ------------------------------------------------------------------

    def _execute_plan(self, record: TaskRecord, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for i, st in enumerate(plan):
            self._emit(
                record,
                "subtask_start",
                {"index": i, "title": st["title"], "total": len(plan)},
            )
            context = _format_context(results)
            system_prompt = SPECIALIST_PROMPT.format(
                title=st["title"],
                instruction=st["instruction"],
                context=context,
                language=_lang_name(self.language),
            )

            def emit_for_subtask(kind: str, data: dict[str, Any], _idx: int = i) -> None:
                self._emit(record, kind, {**data, "subtask": _idx})

            loop = build_loop(
                self.router,
                self.harness.tools,
                system_prompt=system_prompt,
                verbose=self.harness.settings.verbose,
                emit=emit_for_subtask,
            )
            final = loop.invoke(
                {
                    "messages": [{"role": "user", "content": st["instruction"]}],
                    "step": 0,
                    "max_steps": self.max_steps,
                }
            )
            summary = self._final_reply(final.get("messages", [])) or "(no summary)"
            results.append({"index": i, "title": st["title"], "summary": summary})
            self._emit(record, "subtask_done", {"index": i, "title": st["title"], "summary": summary})
        return results

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
