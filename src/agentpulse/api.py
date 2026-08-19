"""FastAPI layer exposing the agentpulse harness over HTTP.

This is the thin "web harness": it holds one Harness instance per process,
accepts chat turns, and returns the reply plus a per-turn tool trace and the
session transcript, so a frontend can render the agent's internal steps.

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
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .harness import Harness

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


def create_app(harness: Harness | None = None) -> FastAPI:
    """App factory; allows tests to inject a harness with a fake router."""
    app = FastAPI(title="agentpulse", version="0.1.0")
    app.state.harness = harness or Harness()

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

    return app


app = create_app()
