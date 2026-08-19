"""Task mode: run the agent loop against an *objective* and stream every
inner step to subscribers (SSE-friendly).

This is the piece that turns agentpulse from a chat box into a task
workbench: the user gives a goal, the agent plans, calls tools, iterates,
and each `thought` / `tool_call` / `tool_result` event is emitted live —
then a final `task_end` event carries the deliverable.

Threading model:
    start()      -> spawns a daemon thread running the (synchronous) loop
    subscribe()  -> returns a queue.Queue the SSE handler drains
    Each event is appended to the task record so late subscribers get the
    full history, then a None sentinel closes the stream.
"""

from __future__ import annotations

import datetime
import json
import queue
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from .graph.loop import build_loop
from .harness import Harness

# Task-mode system prompt: pushes the agent toward plan -> execute -> deliver
# instead of the conversational style used in chat mode.
TASK_SYSTEM_PROMPT = (
    "You are an autonomous task agent. You are given an objective and must "
    "complete it by working through the steps yourself.\n"
    "Rules:\n"
    "1. Start with a short plan (2-5 numbered steps).\n"
    "2. Use tools when they help: write_file/read_file for producing artifacts, "
    "remember/recall for facts, add/get_current_time for computation and time.\n"
    "3. Work step by step; after each tool call, read its result and continue.\n"
    "4. When the objective is fully done, stop calling tools and give a final "
    "deliverable: a clear summary or the artifact you produced.\n"
    "5. Never claim to have done something you did not do. If a tool fails, "
    "report the failure honestly and try a different approach.\n"
    "Be concrete and concise."
)


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
    """Owns task records and executes each objective in a background thread."""

    def __init__(
        self,
        harness: Harness,
        *,
        system_prompt: str = TASK_SYSTEM_PROMPT,
        max_steps: int | None = None,
    ) -> None:
        self.harness = harness
        self.system_prompt = system_prompt
        self.max_steps = max_steps or harness.settings.max_steps
        self._records: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

    # -- public API -----------------------------------------------------------

    def start(self, objective: str) -> str:
        """Create a task and kick off execution in a daemon thread."""
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
        """Return a queue the caller can block on.

        If the task already finished, the queue is pre-filled with the full
        event history plus a None sentinel; if it is still running, the queue
        streams live events and the sentinel arrives at the end.
        """
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

    def _emit(self, record: TaskRecord, event_type: str, data: dict[str, Any]) -> None:
        event: dict[str, Any] = {
            "task_id": record.task_id,
            "type": event_type,
            "data": data,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
        }
        record.events.append(event)
        record.queue.put(event)

    # -- execution --------------------------------------------------------------

    def _run(self, task_id: str) -> None:
        record = self._records[task_id]
        emit: Callable[[str, dict[str, Any]], None] = lambda kind, data: self._emit(record, kind, data)
        try:
            self._emit(record, "task_start", {"objective": record.objective, "model": record.model})

            loop = build_loop(
                self.harness.router,
                self.harness.tools,
                system_prompt=self.system_prompt,
                verbose=self.harness.settings.verbose,
                emit=emit,
            )
            final = loop.invoke(
                {
                    "messages": [{"role": "user", "content": record.objective}],
                    "step": 0,
                    "max_steps": self.max_steps,
                }
            )
            reply = self._final_reply(final.get("messages", []))
            self._emit(
                record,
                "task_end",
                {"reply": reply, "steps": final.get("step", 0)},
            )
            record.status = "done"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            record.error = str(exc)
            record.status = "error"
            self._emit(record, "error", {"message": str(exc)})
        finally:
            record.queue.put(None)

    @staticmethod
    def _final_reply(messages: list[Any]) -> str:
        for m in reversed(messages):
            if isinstance(m, dict):
                if m.get("role") == "assistant" and m.get("content"):
                    return str(m["content"])
            elif getattr(m, "type", "") == "ai" and m.content:
                return str(m.content)
        return ""


def events_to_jsonl(events: list[dict[str, Any]]) -> str:
    """Serialize a task's events as newline-delimited JSON (for tests/dumps)."""
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)
