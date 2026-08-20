"""FastAPI layer exposing the agentpulse harness over HTTP.

This is the thin "web harness": it holds one Harness instance per process,
accepts chat turns, and returns the reply plus a per-turn tool trace and the
session transcript, so a frontend can render the agent's internal steps.
Task mode (TaskRunner) streams every inner step over SSE.

Run (dev):
    uv run uvicorn agentpulse.api:app --reload --port 8000

Endpoints:
    GET  /api/health                 -> service info
    GET  /api/state                  -> {transcript, memories}
    POST /api/chat                   -> {reply, steps, trace}
    POST /api/reset                  -> clear short-term memory
    GET  /api/memories               -> list long-term facts
    POST /api/memories               -> remember(key, value)
    DELETE /api/memories?key=&scope= -> forget
    POST /api/tasks                  -> start a task -> {task_id}
    GET  /api/tasks                  -> list tasks (snapshots)
    GET  /api/tasks/{id}             -> one task snapshot
    GET  /api/tasks/{id}/stream      -> SSE: task_start/thought/tool_call/tool_result/task_end
    GET  /api/sandbox                -> list files in the sandbox
    GET  /api/sandbox/content?path=  -> read a text file from the sandbox
    PUT  /api/sandbox/content?path=  -> write/edit a text file in the sandbox
    DELETE /api/sandbox/file?path=   -> delete a single sandbox file
    DELETE /api/sandbox              -> clear all sandbox files
"""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .harness import Harness
from .tasks import TaskRunner

# Vite dev server default port; the frontend proxies /api here in dev.
_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class MemoryWrite(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    value: Any
    scope: str = "default"


class TaskCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=20000)


class PluginPromote(BaseModel):
    names: list[str] = []


class SandboxWrite(BaseModel):
    content: str = ""


def create_app(
    harness: Harness | None = None,
    runner: TaskRunner | None = None,
) -> FastAPI:
    """App factory; allows tests to inject a harness/runner with fakes."""
    h = harness or Harness()
    app = FastAPI(title="agentpulse", version="0.2.0")
    app.state.harness = h
    app.state.runner = runner or TaskRunner(h)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- routes --------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, str]:
        h: Harness = app.state.harness
        return {"status": "ok", "model": h.settings.model}

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        h: Harness = app.state.harness
        return {
            "transcript": h.transcript,
            "memories": h.long_term.search(scope=h.memory_scope),
            "model": h.settings.model,
        }

    @app.post("/api/chat")
    def chat(req: ChatRequest) -> dict[str, Any]:
        h: Harness = app.state.harness
        try:
            reply = h.run(req.message)
        except Exception as exc:  # noqa: BLE001 - surface LLM errors as 502
            raise HTTPException(status_code=502, detail=f"agent error: {exc}") from exc
        return {
            "reply": reply,
            "steps": h.last_steps,
            "trace": h.last_trace,
            "transcript": h.transcript,
        }

    @app.post("/api/reset")
    def reset() -> dict[str, bool]:
        app.state.harness.reset()
        return {"ok": True}

    @app.get("/api/memories")
    def memories(
        scope: str | None = Query(default=None),
    ) -> dict[str, Any]:
        h: Harness = app.state.harness
        rows = h.long_term.search(scope=scope or h.memory_scope)
        return {"memories": rows}

    @app.post("/api/memories")
    def memory_write(req: MemoryWrite) -> dict[str, bool]:
        h: Harness = app.state.harness
        h.long_term.remember(req.key, req.value, scope=req.scope)
        return {"ok": True}

    @app.delete("/api/memories")
    def memory_delete(
        key: str = Query(min_length=1),
        scope: str = Query(default="default"),
    ) -> dict[str, bool]:
        h: Harness = app.state.harness
        ok = h.long_term.forget(key, scope=scope)
        return {"ok": ok}

    # -- tasks ---------------------------------------------------------------

    @app.post("/api/tasks")
    def task_create(req: TaskCreate) -> dict[str, str]:
        runner: TaskRunner = app.state.runner
        task_id = runner.start(req.objective)
        return {"task_id": task_id}

    @app.get("/api/tasks")
    def task_list() -> dict[str, Any]:
        runner: TaskRunner = app.state.runner
        return {"tasks": runner.list()}

    @app.get("/api/tasks/{task_id}")
    def task_get(task_id: str) -> dict[str, Any]:
        runner: TaskRunner = app.state.runner
        snap = runner.get(task_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="task not found")
        return snap

    @app.get("/api/tasks/{task_id}/stream")
    async def task_stream(task_id: str) -> StreamingResponse:
        runner: TaskRunner = app.state.runner
        q = runner.subscribe(task_id)
        if q is None:
            raise HTTPException(status_code=404, detail="task not found")

        async def event_gen():
            while True:
                # blocking queue read moved off the event loop
                event = await asyncio.to_thread(q.get)
                if event is None:  # sentinel: stream over
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] in {"task_end", "error"}:
                    break

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    _register_plugin_routes(app)
    _register_sandbox_routes(app)
    return app


# -- plugin management (wired into the app factory) -------------------------------


def _register_plugin_routes(app: FastAPI) -> None:
    from .codegen import CodeGenError, delete_plugin, list_plugins, promote_plugins

    @app.get("/api/plugins")
    def plugins_list() -> dict[str, Any]:
        return {"plugins": list_plugins()}

    @app.delete("/api/plugins/{name}")
    def plugin_delete(name: str) -> dict[str, bool]:
        return {"ok": delete_plugin(name)}

    @app.post("/api/plugins/promote")
    def plugin_promote(req: PluginPromote) -> dict[str, Any]:
        try:
            promoted = promote_plugins(req.names)
        except CodeGenError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "promoted": promoted, "file": "src/agentpulse/generated_detectors.py"}

    @app.get("/api/examples")
    def examples() -> dict[str, Any]:
        from .examples import generate_examples

        return {"examples": generate_examples()}

    @app.post("/api/examples/regenerate")
    def examples_regenerate() -> dict[str, Any]:
        from .examples import generate_fresh_examples

        h: Harness = app.state.harness
        return {"examples": generate_fresh_examples(h, force=True)}


# -- sandbox browser (wired into the app factory) -----------------------------


_TEXT_SUFFIXES = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".csv", ".yaml",
    ".yml", ".toml", ".html", ".css", ".sh", ".log", ".rst", ".xml", ".cfg",
    ".ini", ".env", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".sql",
}


def _register_sandbox_routes(app: FastAPI) -> None:
    @app.get("/api/sandbox")
    def sandbox_list() -> dict[str, Any]:
        h: Harness = app.state.harness
        base = Path(h.sandbox_dir) if h.sandbox_dir else None
        files: list[dict[str, Any]] = []
        if base and base.is_dir():
            for p in sorted(base.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(base).as_posix()
                st = p.stat()
                files.append(
                    {
                        "path": rel,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                        "is_text": p.suffix.lower() in _TEXT_SUFFIXES,
                    }
                )
        return {"files": files, "sandbox_dir": str(base) if base else None}

    @app.get("/api/sandbox/content")
    def sandbox_read(path: str = Query(min_length=1)) -> dict[str, Any]:
        h: Harness = app.state.harness
        base = Path(h.sandbox_dir) if h.sandbox_dir else None
        if base is None:
            raise HTTPException(status_code=404, detail="no sandbox configured")
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found in sandbox")
        text = target.read_text(encoding="utf-8", errors="replace")
        return {"path": path, "content": text, "size": target.stat().st_size}

    @app.put("/api/sandbox/content")
    def sandbox_write(
        path: str = Query(min_length=1),
        body: SandboxWrite = ...,
    ) -> dict[str, Any]:
        h: Harness = app.state.harness
        base = Path(h.sandbox_dir) if h.sandbox_dir else None
        if base is None:
            raise HTTPException(status_code=404, detail="no sandbox configured")
        target = (base / path).resolve()
        if not target.is_relative_to(base):
            raise HTTPException(status_code=404, detail="file not found in sandbox")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content, encoding="utf-8")
        return {"path": path, "size": target.stat().st_size, "ok": True}

    @app.delete("/api/sandbox/file")
    def sandbox_delete_file(path: str = Query(min_length=1)) -> dict[str, bool]:
        h: Harness = app.state.harness
        base = Path(h.sandbox_dir) if h.sandbox_dir else None
        if base is None:
            raise HTTPException(status_code=404, detail="no sandbox configured")
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found in sandbox")
        target.unlink()
        return {"ok": True}

    @app.delete("/api/sandbox")
    def sandbox_clear() -> dict[str, Any]:
        h: Harness = app.state.harness
        base = Path(h.sandbox_dir) if h.sandbox_dir else None
        if base is None:
            raise HTTPException(status_code=404, detail="no sandbox configured")
        deleted = 0
        for p in sorted(base.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
                deleted += 1
            elif p.is_dir():
                try:
                    p.rmdir()  # non-empty dirs are skipped (OSError)
                except OSError:
                    pass
        return {"ok": True, "deleted": deleted}


app = create_app()
