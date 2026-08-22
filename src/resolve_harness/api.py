"""FastAPI layer exposing the resolve_harness harness over HTTP.

This is the thin "web harness": it holds one Harness instance per process,
accepts chat turns, and returns the reply plus a per-turn tool trace and the
session transcript, so a frontend can render the agent's internal steps.
Task mode (TaskRunner) streams every inner step over SSE.

Run (dev):
    uv run uvicorn resolve_harness.api:app --reload --port 8000

Endpoints:
    GET  /api/health                 -> service info
    GET  /api/state                  -> {transcript, memories}
    POST /api/chat                   -> {reply, steps, trace, status, pending?, thread_id?}
    GET  /api/chat/history           -> recent chat turns with token usage
    GET  /api/tools                  -> every tool + which modes expose it
    GET  /api/agents                 -> task-pipeline agents + graph layout
    GET  /api/config                  -> runtime config (task parallel count)
    PUT  /api/config                  -> update + persist runtime config
    POST /api/chat/approve           -> resume a suspended approval turn (same shape as /chat)
    POST /api/reset                  -> clear short-term memory
    GET  /api/memories               -> list long-term facts
    POST /api/memories               -> remember(key, value)
    DELETE /api/memories?key=&scope= -> forget one (no key = clear whole scope)
    POST /api/tasks                  -> start a task -> {task_id}
    GET  /api/tasks                  -> list tasks (snapshots)
    GET  /api/tasks/{id}             -> one task snapshot
    GET  /api/tasks/{id}/stream      -> SSE: task_start/thought/tool_call/tool_result/task_end
    GET  /api/sandbox                -> list files in the sandbox
    GET  /api/sandbox/raw?path=      -> raw bytes with proper Content-Type (img/iframe)
    GET  /api/sandbox/content?path=  -> read a text file from the sandbox
    PUT  /api/sandbox/content?path=  -> write/edit a text file in the sandbox
    DELETE /api/sandbox/file?path=   -> delete a single sandbox file
    DELETE /api/sandbox              -> clear all sandbox files
    POST /api/examples/delete        -> tombstone an example (built-in or generated)
"""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .chat_history import ChatHistoryStore
from .event_log import EventLog
from .harness import Harness, _default_sandbox_dir
from .llm import LLMError, usage_diff, usage_snapshot
from .tasks import TaskRunner

# Vite dev server default port; the frontend proxies /api here in dev.
_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # 会话级模型覆盖（本 turn 用该模型，别名或模型名；空 = 聊天模型默认）
    model: str | None = Field(default=None, max_length=200)


class MemoryWrite(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    value: Any
    scope: str = "default"


class TaskCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=20000)


class ConfigUpdate(BaseModel):
    # None = 未变更：不改内存、不覆盖磁盘（支持字段级部分更新，避免 UI
    # 全量提交把磁盘上被脚本/手动改过的其它字段覆盖回内存旧值）。
    parallel: int | None = Field(default=None, ge=1, le=16)
    max_replan_rounds: int | None = Field(default=None, ge=0, le=5)
    max_steps: int | None = Field(default=None, ge=1, le=50)
    agent_models: dict[str, str] | None = Field(
        default=None,
        description='per-agent LLM override: planner/specialist/evaluator/reporter '
        '(None = keep current) — task model, empty = .env baseline',
    )
    default_model: str | None = Field(
        default=None,
        description="chat model override ('' = clear, back to .env LLM_MODEL; "
        'None = keep current) — chat only, independent of task model',
    )
    models: dict[str, dict[str, str]] | None = Field(
        default=None,
        description="named model profiles {alias: {base_url, model, api_key_env}} "
        '(None = keep current)',
    )


def _config_path() -> Path:
    """Runtime config file: data/config.json (parallel count, replan rounds,
    specialist loop step cap, per-agent LLM overrides, default model)."""
    here = Path(__file__).resolve().parent
    root = here.parent.parent
    return root / "data" / "config.json"


_AGENT_KEYS = ("planner", "specialist", "evaluator", "reporter")

_DEFAULT_CONFIG: dict[str, Any] = {
    "parallel": 4,  # Specialist fan-out
    "max_replan_rounds": 1,  # evaluator-fail replan rounds (anti-runaway)
    "max_steps": 10,  # specialist tool-loop step cap (planner/evaluator are single calls)
    "agent_models": {},  # 任务模型：per-agent LLM override (empty = .env baseline)
    "default_model": "",  # 聊天模型：chat model override (empty = .env LLM_MODEL)
    "models": {},  # named model profiles {alias: {base_url, model, api_key_env}}
    "sandbox_dir": "",  # 沙箱根目录（空 = 默认 data/sandbox）；可在沙箱 Tab 热更新
    "sandbox_history": [],  # 记录已使用过的沙箱目录（最新在前），方便切回
}


def _load_config() -> dict[str, Any]:
    """Runtime config: parallel (4), replan rounds (1), steps (10),
    agent models, default model, model profiles."""
    cfg = json.loads(json.dumps(_DEFAULT_CONFIG))
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
    except OSError:
        return cfg
    for key in ("parallel", "max_replan_rounds", "max_steps"):
        try:
            cfg[key] = int(raw[key])
        except (KeyError, ValueError, TypeError):
            pass
    am = raw.get("agent_models") or {}
    if isinstance(am, dict):
        cfg["agent_models"] = {k: str(v) for k, v in am.items() if k in _AGENT_KEYS and v}
    dm = raw.get("default_model")
    if isinstance(dm, str):
        cfg["default_model"] = dm.strip()
    mp = raw.get("models") or {}
    if isinstance(mp, dict):
        cleaned: dict[str, dict[str, str]] = {}
        for alias, spec in mp.items():
            if not isinstance(spec, dict):
                continue
            s = {
                k: str(spec.get(k) or "").strip()
                for k in ("base_url", "model", "api_key_env")
                if spec.get(k)
            }
            if s:
                cleaned[str(alias).strip()] = s
        cfg["models"] = cleaned
    sd = raw.get("sandbox_dir")
    if isinstance(sd, str) and sd.strip():
        cfg["sandbox_dir"] = sd.strip()
    hist = raw.get("sandbox_history")
    if isinstance(hist, list):
        cfg["sandbox_history"] = [str(x) for x in hist if isinstance(x, str) and x]
    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


class PluginPromote(BaseModel):
    names: list[str] = []


class ExampleDelete(BaseModel):
    label: str = Field(default="", max_length=200)
    text: str = Field(min_length=1, max_length=5000)


class SandboxWrite(BaseModel):
    content: str = ""


class SandboxLocation(BaseModel):
    """Set the sandbox root directory at runtime (Sandbox tab hot-switch)."""

    path: str = Field(min_length=1)


# 复位到项目默认沙箱（data/sandbox）的哨兵值，由前端「项目沙箱」快捷按钮发送。
DEFAULT_SANDBOX_SENTINEL = "__default__"


class ApprovalDecision(BaseModel):
    """One human decision for a pending tool call.

    `action` is approve / deny / edit; `edit` carries replacement `args`;
    `deny` may carry a `reason`. A bare "approve"/"deny" string (the
    `ApprovalRequest.decisions` shortcut) applies to every pending call.
    """

    id: str = Field(min_length=1)
    action: str
    reason: str | None = None
    args: dict[str, Any] | None = None


class ApprovalRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    # list of per-call decisions, OR a bare "approve"/"deny" string
    decisions: list[ApprovalDecision] | str


def _chat_payload(
    h: Harness, reply: str, usage: dict[str, int] | None = None
) -> dict[str, Any]:
    """Build the chat/approve response, surfacing a pending approval gate.

    When the harness suspended on an approval interrupt, `status` is
    `pending_approval` and `pending` lists the waiting tool calls plus the
    `thread_id` the caller must echo back to `/api/chat/approve`.
    """
    pending = getattr(h, "pending_approval", None)
    return {
        "reply": reply,
        "steps": h.last_steps,
        "trace": h.last_trace,
        "transcript": h.transcript,
        "status": "pending_approval" if pending else "done",
        "pending": pending["tool_calls"] if pending else None,
        "thread_id": pending["thread_id"] if pending else None,
        "usage": usage,
    }


def _record_chat_turn(app: FastAPI, user_msg: str, payload: dict[str, Any]) -> None:
    """Persist a completed chat turn (status=done) with its token usage.

    Suspended turns (pending_approval) are intermediate — the final usage is
    only known after the approve resume, so only `done` payloads are logged.
    Best-effort: storage failures never break the chat response.
    """
    if payload.get("status") != "done":
        return
    usage = payload.get("usage") or {}
    try:
        store: ChatHistoryStore = app.state.chat_history
        store.record(
            user_msg=user_msg,
            reply=str(payload.get("reply") or ""),
            steps=int(payload.get("steps") or 0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass


def create_app(
    harness: Harness | None = None,
    runner: TaskRunner | None = None,
    chat_history: ChatHistoryStore | None = None,
) -> FastAPI:
    """App factory; allows tests to inject a harness/runner with fakes."""
    _cfg = _load_config()
    h = harness or Harness(sandbox_dir=_cfg.get("sandbox_dir") or None)
    app = FastAPI(title="Resolve Harness", version="0.2.0")
    app.state.harness = h
    app.state.runner = runner or TaskRunner(h)
    # .env LLM_MODEL baseline — clearing a default_model override falls back here
    try:
        app.state.env_model = app.state.runner.router.settings.model
    except AttributeError:  # tests inject fake runners without a router
        app.state.env_model = ""
    # apply persisted runtime config (parallel fan-out, replan rounds, steps,
    # per-agent LLM overrides, default model)
    app.state.runner.parallel = _cfg["parallel"]
    app.state.runner.max_replan_rounds = _cfg["max_replan_rounds"]
    app.state.runner.max_steps = _cfg["max_steps"]
    app.state.runner.set_agent_models(_cfg["agent_models"])
    _set_models = getattr(app.state.runner.router, "set_models", None)
    if _set_models:
        _set_models(_cfg["models"])
    if _cfg["default_model"]:
        app.state.runner.router.settings.model = _cfg["default_model"]
    app.state.chat_history = chat_history or ChatHistoryStore()
    # 事件日志：复用 harness 的连接（单一数据源）；无 harness 时自建
    app.state.event_log = getattr(app.state.harness, "_event_log", None) or EventLog()
    # 已使用过的沙箱目录历史（沙箱 Tab 热切换用）
    app.state.sandbox_history = _cfg.get("sandbox_history", [])

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
        before = usage_snapshot(h.router)
        try:
            # chat UI: each turn is stateless (no session history sent to the
            # model) — cross-turn knowledge comes from long-term memory +
            # the automatic memory pre-fetch; the transcript still displays
            # the full conversation.
            reply = h.run(req.message, session_history=False, model=req.model)
        except LLMError as exc:
            # LLMError 已是被压缩过的友好提示（含可操作建议）；对调用方属客户端
            # 错误，返回 400，而不是把 traceback 包成 502。
            raise HTTPException(
                status_code=400,
                detail=f"模型调用失败：{exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface other errors as 502
            raise HTTPException(status_code=502, detail=f"agent error: {exc}") from exc
        payload = _chat_payload(h, reply, usage_diff(h.router, before))
        _record_chat_turn(app, req.message, payload)
        return payload

    @app.get("/api/chat/history")
    def chat_history() -> dict[str, Any]:
        store: ChatHistoryStore = app.state.chat_history
        return {"turns": store.recent()}

    @app.get("/api/tools")
    def tools() -> dict[str, Any]:
        """List every tool the harness provides, with which modes expose it.

        `chat` = interactive chat loop; `task` = the task pipeline's per-task
        registry (fs tools are re-bound to the task sandbox, same names).
        """
        h: Harness = app.state.harness
        chat_names = {t.name for t in h.chat_tools}
        task_excluded = {"remember", "list_memories"}  # tasks may recall, never write
        out = []
        for t in h.tools:
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "require_approval": bool(t.require_approval),
                    "chat": t.name in chat_names,
                    "task": t.name not in task_excluded,
                }
            )
        return {"tools": out}

    @app.get("/api/agents")
    def agents() -> dict[str, Any]:
        """Task-pipeline agents + the graph layout (for the Agent tab).

        Mirrors the orchestrator in tasks.py: Planner -> Specialist(s) ->
        Evaluator -> (pass: Reporter | fail: re-plan) -> end, with Fast Path
        and codegen short-circuits and an error sink.
        """
        nodes = [
            # 左列主链：开始 → Planner → Specialist ×N → Evaluator → 判定
            {"id": "start", "label": "开始", "x": 40, "y": 30, "shape": "ellipse", "kind": "entry"},
            {"id": "plan", "label": "Planner\n拆解目标", "x": 40, "y": 180, "shape": "rect", "kind": "agent"},
            {"id": "execute", "label": "Specialist ×N\n执行子任务", "x": 40, "y": 330, "shape": "rect", "kind": "agent"},
            {"id": "evaluate", "label": "Evaluator\n评估结果", "x": 40, "y": 480, "shape": "rect", "kind": "agent"},
            # 中列判定与收口：达标？→ Reporter → 结束
            {"id": "decision", "label": "达标？", "x": 280, "y": 480, "shape": "diamond", "kind": "gate"},
            {"id": "report", "label": "Reporter\n汇总交付", "x": 280, "y": 600, "shape": "rect", "kind": "agent"},
            # 右列短路与出口：Fast Path / codegen → 结束
            {"id": "fastpath", "label": "Fast Path\n代码直算", "x": 520, "y": 30, "shape": "rect", "kind": "shortcut"},
            {"id": "codegen", "label": "codegen\n生成检测器", "x": 520, "y": 140, "shape": "rect", "kind": "shortcut"},
            {"id": "end", "label": "结束", "x": 520, "y": 600, "shape": "ellipse", "kind": "exit"},
            # 异常出口
            {"id": "error", "label": "错误", "x": 40, "y": 660, "shape": "rect", "kind": "error"},
        ]
        edges = [
            # 快捷分支（右列）
            {"from": "start", "to": "fastpath", "label": "objective 命中\n确定性查询"},
            {"from": "fastpath", "to": "end", "label": "代码直算\n零模型"},
            {"from": "start", "to": "codegen", "label": "未命中"},
            {"from": "codegen", "to": "end", "label": "成功即持久化\n复用"},
            # 主链
            {"from": "start", "to": "plan", "label": "常规路径"},
            {"from": "plan", "to": "execute", "label": "子任务列表\n并行 / 串行"},
            {"from": "execute", "to": "evaluate", "label": "各子任务结果"},
            {"from": "evaluate", "to": "decision", "label": "对照目标验证"},
            # 判定分支
            {"from": "decision", "to": "report", "label": "通过"},
            {"from": "decision", "to": "plan", "label": "未通过（带反馈\n重规划，限轮次）", "loop": True},
            {"from": "report", "to": "end", "label": "最终交付"},
            # 异常出口：plan→execute→evaluate→replan 任一环节抛异常都会落入 error
            #（tasks.py 顶层 try/except 置 status="error"），error 为红色失败终止节点。
            {"from": "evaluate", "to": "error", "label": "异常"},
        ]
        agents = [
            {
                "name": "Planner",
                "role": "拆解目标",
                "description": "把任务目标拆成可执行子任务计划（JSON 数组），自己不执行任何工作；是唯一决定『做什么』的节点。",
                "tools": "无（纯 LLM）",
                "phase": "plan",
            },
            {
                "name": "Specialist",
                "role": "执行子任务",
                "description": "完成单个子任务：用工具循环干活（沙箱文件 / recall 读记忆 / fetch / 当前时间），结果写入自己的沙箱；只读记忆、禁写长期记忆。",
                "tools": "write_file · read_file · list_files · recall · fetch · get_current_time",
                "phase": "execute",
            },
            {
                "name": "Evaluator",
                "role": "评估结果",
                "description": "对照原始目标验证子任务结果是否真正达标，输出 passed / score / feedback；未达标时把反馈交给 Planner 重规划（限轮次）。",
                "tools": "无（纯 LLM）",
                "phase": "evaluate",
            },
            {
                "name": "Reporter",
                "role": "汇总交付",
                "description": "把已完成的执行结果汇总成最终交付物（Markdown），是整个任务的收口。",
                "tools": "无（纯 LLM）",
                "phase": "report",
            },
        ]
        return {"agents": agents, "graph": {"nodes": nodes, "edges": edges}}

    @app.get("/api/config")
    def config_get() -> dict[str, Any]:
        runner: TaskRunner = app.state.runner
        cfg = _load_config()
        return {
            "parallel": runner.parallel,
            "max_replan_rounds": runner.max_replan_rounds,
            "max_steps": runner.max_steps,
            "agent_models": runner.agent_models,
            "default_model": cfg.get("default_model", ""),
            "active_model": runner.router.settings.model,
            "models": cfg.get("models", {}),
            "env_model": {
                "base_url": runner.router.settings.api_base or "",
                "model": app.state.env_model,
                "api_key_env": "LLM_API_KEY",
            },
        }

    @app.put("/api/config")
    def config_update(req: ConfigUpdate) -> dict[str, Any]:
        """Update task runtime config (parallel fan-out, replan rounds, loop
        step cap, per-agent LLM overrides, default model, model profiles);
        applies to the next task and persists across restarts. Optional
        fields (agent_models/default_model/models=None) keep current values."""
        runner: TaskRunner = app.state.runner
        if req.parallel is not None:
            runner.parallel = req.parallel
        if req.max_replan_rounds is not None:
            runner.max_replan_rounds = req.max_replan_rounds
        if req.max_steps is not None:
            runner.max_steps = req.max_steps
        if req.agent_models is not None:
            runner.set_agent_models(req.agent_models)
        if req.models is not None:
            runner.router.set_models(req.models)
        if req.default_model is not None:
            # empty -> back to .env LLM_MODEL; non-empty -> live override
            runner.router.settings.model = req.default_model or app.state.env_model
        prev = _load_config()
        # 写盘：仅显式变更的字段覆盖，其余保留磁盘现值（prev）。
        _save_config(
            {
                "parallel": req.parallel
                if req.parallel is not None
                else prev.get("parallel", _DEFAULT_CONFIG["parallel"]),
                "max_replan_rounds": req.max_replan_rounds
                if req.max_replan_rounds is not None
                else prev.get("max_replan_rounds", _DEFAULT_CONFIG["max_replan_rounds"]),
                "max_steps": req.max_steps
                if req.max_steps is not None
                else prev.get("max_steps", _DEFAULT_CONFIG["max_steps"]),
                "agent_models": prev.get("agent_models", {})
                if req.agent_models is None
                else req.agent_models,
                "default_model": prev.get("default_model", "")
                if req.default_model is None
                else req.default_model,
                "models": prev.get("models", {}) if req.models is None else req.models,
            }
        )
        cur = _load_config()
        return {
            "parallel": runner.parallel,
            "max_replan_rounds": runner.max_replan_rounds,
            "max_steps": runner.max_steps,
            "agent_models": runner.agent_models,
            "default_model": cur.get("default_model", ""),
            "active_model": runner.router.settings.model,
            "models": cur.get("models", {}),
        }

    @app.delete("/api/chat/history")
    def chat_history_clear() -> dict[str, Any]:
        store: ChatHistoryStore = app.state.chat_history
        return {"deleted": store.clear()}

    @app.post("/api/chat/approve")
    def chat_approve(req: ApprovalRequest) -> dict[str, Any]:
        """Resume a turn suspended on the approval gate with the human's decisions.

        Returns the same shape as /api/chat — which may be `pending_approval`
        again when the resumed turn hits a later approval-gated call (chained
        approval); the caller resolves that one with another approve call.
        """
        h: Harness = app.state.harness
        pending = h.pending_approval
        if pending is None or req.thread_id != pending["thread_id"]:
            raise HTTPException(
                status_code=409,
                detail="no pending approval for this thread_id",
            )
        before = usage_snapshot(h.router)
        try:
            # req.decisions 是 pydantic 模型列表（或裸字符串）；human_gate 内部的
            # _normalize_decisions 只认 dict，故在边界把模型转成 dict，否则逐条被跳过、
            # 所有待审调用默认拒绝。裸字符串路径原样透传。
            decisions = req.decisions
            if isinstance(decisions, list):
                decisions = [d.model_dump() for d in decisions]
            reply = h.resolve_approval(decisions)
        except LLMError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"模型调用失败：{exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface other errors as 502
            raise HTTPException(status_code=502, detail=f"agent error: {exc}") from exc
        payload = _chat_payload(h, reply, usage_diff(h.router, before))
        _record_chat_turn(app, "(resume approval)", payload)
        return payload

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
        key: str | None = Query(default=None, min_length=1),
        scope: str = Query(default="default"),
    ) -> dict[str, Any]:
        """Delete one memory by key, or clear a whole scope when key is absent."""
        h: Harness = app.state.harness
        if key is None:
            return {"ok": True, "deleted": h.long_term.clear(scope=scope)}
        return {"ok": h.long_term.forget(key, scope=scope)}

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

    @app.delete("/api/tasks")
    def task_clear() -> dict[str, Any]:
        """清空任务历史（含持久化与内存中的任务）。"""
        runner: TaskRunner = app.state.runner
        return {"deleted": runner.clear_history()}

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
                if event["type"] in {"task_end", "error", "task_stopped"}:
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

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict[str, Any]:
        """请求后端中止一个运行中的任务（best-effort，安全点生效）。"""
        runner: TaskRunner = app.state.runner
        ok = runner.stop(task_id)
        return {"ok": ok}

    _register_plugin_routes(app)
    _register_sandbox_routes(app)
    return app


# -- plugin management (wired into the app factory) -------------------------------


def _register_plugin_routes(app: FastAPI) -> None:
    from .codegen import (
        CodeGenError,
        delete_plugin,
        list_builtin_detectors,
        list_plugins,
        promote_plugins,
    )
    from .fastpath import list_builtin_matchers

    @app.get("/api/events")
    def events(
        scope: str | None = Query(default=None, pattern="^(chat|task)$"),
        ref: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> dict[str, Any]:
        """事件日志查询（聊天/任务事件持久化，可回放/审计）。

        带 `ref`：返回该 turn/task 的完整 trace（原始顺序，replay）。
        不带 `ref`：返回 scope 的最近事件 + 可回放 refs 列表。
        """
        log: EventLog = app.state.event_log
        if ref:
            return {"refs": [], "events": log.replay(scope or "task", ref)}
        return {
            "refs": log.refs(scope, limit) if scope else [],
            "events": log.recent(scope, limit),
        }

    @app.delete("/api/events")
    def events_clear() -> dict[str, Any]:
        """清空事件日志（审计）：删除全部聊天/任务事件。"""
        log: EventLog = app.state.event_log
        return {"deleted": log.clear()}

    @app.get("/api/plugins")
    def plugins_list() -> dict[str, Any]:
        # core matchers + runtime plugins + promoted built-in detectors
        return {
            "plugins": [
                *list_builtin_matchers(),
                *list_plugins(),
                *list_builtin_detectors(),
            ]
        }

    @app.delete("/api/plugins/{name}")
    def plugin_delete(name: str) -> dict[str, bool]:
        return {"ok": delete_plugin(name)}

    @app.post("/api/plugins/promote")
    def plugin_promote(req: PluginPromote) -> dict[str, Any]:
        try:
            promoted = promote_plugins(req.names)
        except CodeGenError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "promoted": promoted, "file": "src/resolve_harness/generated_detectors.py"}

    @app.get("/api/examples")
    def examples() -> dict[str, Any]:
        from .examples import generate_examples

        return {"examples": generate_examples()}

    @app.post("/api/examples/regenerate")
    def examples_regenerate() -> dict[str, Any]:
        from .examples import generate_fresh_examples

        h: Harness = app.state.harness
        return {"examples": generate_fresh_examples(h, force=True)}

    @app.post("/api/examples/delete")
    def examples_delete(req: ExampleDelete) -> dict[str, bool]:
        from .examples import delete_example

        return {"ok": delete_example(req.label, req.text)}


# -- sandbox browser (wired into the app factory) -----------------------------


_TEXT_SUFFIXES = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".csv", ".yaml",
    ".yml", ".toml", ".html", ".css", ".sh", ".log", ".rst", ".xml", ".cfg",
    ".ini", ".env", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".sql",
}

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif"}
_MD_SUFFIXES = {".md", ".markdown"}
_HTML_SUFFIXES = {".html", ".htm"}
_PDF_SUFFIXES = {".pdf"}
_CSV_SUFFIXES = {".csv", ".tsv"}
_CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".sh", ".bash", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp",
    ".sql", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".rb", ".php", ".swift",
}


def _file_kind(suffix: str) -> str:
    """Classify a file by extension for the previewer: text/markdown/html/image/pdf/csv/json/code/binary."""
    if suffix in _MD_SUFFIXES:
        return "markdown"
    if suffix in _HTML_SUFFIXES:
        return "html"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _PDF_SUFFIXES:
        return "pdf"
    if suffix in _CSV_SUFFIXES:
        return "csv"
    if suffix == ".json":
        return "json"
    if suffix in _CODE_SUFFIXES:
        return "code"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    return "binary"


def _media_type(name: str) -> str:
    import mimetypes

    if name.lower().endswith(".svg"):
        return "image/svg+xml"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _register_sandbox_routes(app: FastAPI) -> None:
    @app.get("/api/sandbox")
    def sandbox_list() -> dict[str, Any]:
        h: Harness = app.state.harness
        base = Path(h.sandbox_dir) if h.sandbox_dir else None
        files: list[dict[str, Any]] = []
        if base and base.is_dir():
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                st = p.stat()
                files.append(
                    {
                        "path": p.relative_to(base).as_posix(),
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                        "is_text": p.suffix.lower() in _TEXT_SUFFIXES,
                        "kind": _file_kind(p.suffix.lower()),
                    }
                )
            # newest first — the agent just wrote something → it surfaces on top
            files.sort(key=lambda f: f["mtime"], reverse=True)
        return {
            "files": files,
            "sandbox_dir": str(base) if base else None,
            "sandbox_history": list(app.state.sandbox_history),
        }

    @app.get("/api/sandbox/raw")
    def sandbox_raw(path: str = Query(min_length=1)) -> Response:
        """Raw bytes of a sandbox file (images, PDFs, HTML...) with a proper
        Content-Type, so <img>/<iframe> can render it directly."""
        h: Harness = app.state.harness
        base = Path(h.sandbox_dir) if h.sandbox_dir else None
        if base is None:
            raise HTTPException(status_code=404, detail="no sandbox configured")
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found in sandbox")
        return Response(content=target.read_bytes(), media_type=_media_type(target.name))

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

    @app.put("/api/sandbox/location")
    def sandbox_set_location(req: SandboxLocation) -> dict[str, Any]:
        """热切换沙箱根目录（沙箱 Tab）：更新 harness 并把重绑后的工具生效，
        记录到历史（去重、最新在前）并持久化到 data/config.json。"""
        h: Harness = app.state.harness
        target = (
            _default_sandbox_dir()
            if req.path == DEFAULT_SANDBOX_SENTINEL
            else Path(req.path).expanduser()
        )
        try:
            h.set_sandbox_dir(str(target))
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"无法创建/访问目录：{exc}")
        new_dir = target.resolve()
        history = [str(new_dir)] + [
            p for p in app.state.sandbox_history if p != str(new_dir)
        ]
        history = history[:20]
        app.state.sandbox_history = history
        cfg = _load_config()
        cfg["sandbox_dir"] = str(new_dir)
        cfg["sandbox_history"] = history
        _save_config(cfg)
        return {"sandbox_dir": str(new_dir), "sandbox_history": history}

    @app.delete("/api/sandbox/history")
    def sandbox_delete_history(path: str = Query(min_length=1)) -> dict[str, Any]:
        """从「已用目录」历史中移除某条记录（不影响当前沙箱根目录本身）。"""
        history = [p for p in app.state.sandbox_history if p != path]
        app.state.sandbox_history = history
        cfg = _load_config()
        cfg["sandbox_history"] = history
        _save_config(cfg)
        return {"sandbox_history": history}

    @app.delete("/api/sandbox/history/all")
    def sandbox_clear_history() -> dict[str, Any]:
        """清空「已用目录」历史。"""
        app.state.sandbox_history = []
        cfg = _load_config()
        cfg["sandbox_history"] = []
        _save_config(cfg)
        return {"sandbox_history": []}


app = create_app()
