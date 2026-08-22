# resolve_harness

A minimal Python AI Agent project skeleton: a **LangGraph** orchestration loop, **LiteLLM** unified model routing (Harness), **layered memory**, and a **deterministic fast path** (Fast Path: if it can be computed, the model never should). Dependencies and runtime are managed with `uv`.

The design goal isn't "yet another agent framework" but to **break apart, explain, and make replaceable** the key mechanisms of an agent:

| Pillar | Location | Responsibility |
|---|---|---|
| **Loop** | `src/resolve_harness/graph/` | LangGraph StateGraph: `agent → tools → agent…`, with `max_steps` to prevent runaway |
| **Harness** | `src/resolve_harness/harness.py` | Single entry point: assembling config, model routing, tool registry, memory, loop |
| **Memory** | `src/resolve_harness/memory/` | Short-term (session transcript) + long-term (SQLite facts, read/written via tools) |
| **Fast Path** | `src/resolve_harness/fastpath.py` + `codegen.py` | Deterministic queries solved purely in code: a hit means zero model calls; on a miss, ask the model to generate a detector and persist it for reuse |

## ✨ Core Highlight: the Fast-path Plugin Runtime (model-written · zero-model reuse · promotable into source)

Most "tasks" are actually deterministic — doing arithmetic, converting bases, looking up an exchange rate — and running the LLM repeatedly on them is slow, costly, and error-prone. resolve_harness strips that part out entirely with a three-tier fast path, and **the whole process is fully transparent to the UI / memory / orchestration loop** (the task tree still renders normally and is labeled "zero-model"):

1. **Built-in matchers** (`fastpath.py`): arithmetic / time / unit conversion / date math / base conversion / text stats / index quotes… a hit means pure-code computation, **zero model calls**.
2. **Codegen (let the model write its own fast path)**: on a miss, the harness asks the model "can this be solved deterministically with a pure Python function"; if so it generates `detect(text) -> str | None`, which — after sandbox validation via an AST whitelist — is **persisted** to `data/fastpath_plugins/`, reused immediately this time, and hit directly next time the same question comes up.
3. **Promote**: in the "Plugins" tab, select a well-behaved detector and "promote" it — this merges it into `src/resolve_harness/generated_detectors.py`, turning it into a built-in detector committed with the source; the runtime copy is then removed.

End to end: **develop → persist → reuse → graduate**. Effect: the first time you ask "is 2024 a leap year" the model writes the detector on the spot; afterwards, asking the same kind of question doesn't even call the model. See the "Fast Path" and "Codegen" sections below, plus the promotion notes in the "Plugins" tab.

## Quick Start

```bash
cd work/harness/resolve_harness
cp .env.example .env          # fill in LLM_MODEL / LLM_API_KEY
uv sync                        # install dependencies (auto-uses the Tsinghua mirror in China)
uv run resolve_harness-chat         # interactive chat (terminal)
```

The full pipeline runs without a real key: `uv run pytest` drives the loop with a FakeRouter, touching no network.

```bash
uv run pytest                 # offline unit tests (memory / tools / loop / api / tasks / fastpath / codegen)
uv run python examples/tool_demo.py   # script demo: time / arithmetic / memory
```

### Web UI (Vite + React)

One-command start of both frontend and backend (recommended):

```bash
make dev
```

`make dev` will: ① first clean up leftover processes occupying `:8000` / `:5173` → ② start the FastAPI backend and wait for health check → ③ then start the Vite frontend → open http://localhost:5173. Ctrl-C stops both at once.

You can also start them manually in two terminals:

```bash
# Terminal 1: FastAPI backend (http://127.0.0.1:8000, interactive docs at /docs)
make api

# Terminal 2: Vite frontend (http://localhost:5173)
cd web && pnpm install        # first time
make web-dev
```

Open http://localhost:5173 to use it. There are **eight tabs** in total (Chat is the default, first one):

- **Tasks** (multi-agent workbench): the top shows example cards (built-in + a "regenerate" button that asks the model to generate a fresh batch of executable examples); built-in examples cover arithmetic / docs / code / web scraping (overseas JD listings, A-share live quotes, USD exchange rate — all via the `fetch` tool); cards can be single-click to fill, double-click to run directly, or hover to delete — deletion is persisted (both built-in and generated can be deleted and won't reappear after refresh), and deleted examples are injected as a "negative list" into the regenerate prompt to stop the model producing similar tasks; after entering a goal, **Planner breaks it into subtasks → Specialists execute one by one (tool loop) → Evaluator accepts** (auto-sent back for replanning on failure, at most 1 round) → **Reporter compiles the deliverable**. Every step (plan / subtask progress / thought / tool call / acceptance conclusion) streams live as a task tree via SSE, and the deliverable supports markdown. If the goal is a deterministic query (e.g. "compute 2+3"), it goes straight through the Fast Path and returns instantly, with the task tree still fully shown and labeled "zero-model".
- **Chat**: one question one answer; tool calls show as small tags below the reply; deterministic queries also go through Fast Path, no need to wait for the model.
- **History**: persisted successful tasks (`data/task_history.db`) listed newest-first; click any one to replay the full task tree (plan / subtasks / LangGraph loop graph / tool calls / produced files / deliverable), also supports "save to long-term memory".
- **Plugins**: manage runtime-generated fast-path plugins — view source, delete individually, or "promote" to merge a well-behaved plugin into `src/resolve_harness/generated_detectors.py` as a built-in detector (committed with source).
- **Tools**: list all tools provided by the project (`GET /api/tools`) — each tool's name, description, parameters (including required), approval flag, and which modes it's available in (chat / task), with filtering by mode.
- **Agent**: cards for the four task-pipeline roles (Planner / Specialist / Evaluator / Reporter) + an Orchestrator flow SVG (with Fast Path / codegen short-circuit, pass branch and failure-replan loop).
- **Sandbox**: browse files under `data/sandbox/` (newest on top by modification time), support single delete and clear-all; preview renders by type — markdown rendered as doc, CSV/TSV as table, HTML in iframe, images shown directly, PDF embedded, JSON auto-formatted + syntax highlighted, code (py/js/ts/go/rust…) syntax highlighted, text types can switch back to source edit, plain text keeps source view.
- **Settings**: runtime config — model library (multiple named models, each with Base URL / model name / API Key env var name, key body in .env not on disk), global default model (overrides .env `LLM_MODEL`, can clear to fall back), the four agents (Planner/Specialist/Evaluator/Reporter) each pick a model from the library (blank follows default), Specialist loop step cap / parallel subtask count N / failure retry rounds. All changes take effect immediately (from next task) and persist to `data/config.json` (`GET/PUT /api/config`).

The right sidebar lists long-term memory live (deletable), and the top "clear session" resets short-term memory. Vite dev has `/api` proxied to the backend, no CORS handling needed.

## Architecture

```
┌────────────────────────────── Harness ─────────────────────────────┐
│                                                                     │
│  Settings  ──►  LiteLLMRouter  ──►  any provider / compatible endpoint│
│  (.env)                                                            │
│                                                                     │
│  Fast Path  hit deterministic query (arithmetic/time/convert/…) → answer directly, zero model │
│  Codegen    on miss, ask model to write detector, persist after validation → zero model next time │
│                                                                     │
│  ShortTermMemory  ◄──  Loop (LangGraph StateGraph)  ──►  ToolRegistry│
│  (session transcript, bounded)  agent ──► tools ──► agent ──► END    │
│                       agent ──(approval-needed tool)──► human_gate  │
│                       (interrupt suspend ⇄ Command(resume) resume)  │
│                       max_steps hard cap                            │
│  LongTermMemory  ◄──────────────────────────────────────┘          │
│  (SQLite facts, cross-session)   ↑ remember / recall / list_memories tools │
└────────────────────────────────────────────────────────────────────┘
```

The journey of one `harness.run(text)`:

1. **Fast Path**: try pure code first — arithmetic, current time, statistics, unit conversion, date math, base conversion, character stats, sandbox file reads. A hit returns immediately, zero model calls;
2. **Codegen**: on a miss, ask the model "can this be solved deterministically with a pure Python function"; if so, generate a detector, run it after sandbox validation, and on success persist it as a plugin (zero model next time for the same question);
3. **Loop**: still unresolved → enter the LangGraph loop. The user message plus history transcript is the initial state; the `agent` node produces a reply or `tool_calls`; when there are tool calls and `step < max_steps`, route to the `tools` node to execute, inject the result back, and return to `agent` to keep reasoning;
4. The final reply is written back to short-term memory; facts the agent writes via `remember` go into long-term memory.

## Core Design

### Loop (`graph/loop.py`)

- Hand-written StateGraph, not the prebuilt `ToolNode` — every decision point in the loop is visible and changeable;
- `AgentState` uses LangGraph's `add_messages` reducer for message accumulation and tool-result pairing;
- **History trimming before sending to the LLM** (`_trim_history`, cap `MAX_LLM_MESSAGES=24`): multi-turn history is resent in full each round, so older rounds are trimmed to control token usage; the cut point never lands on a `ToolMessage` (it auto-extends backward to include the paired assistant tool call, keeping OpenAI compatibility), and long-term facts can still be retrieved via the `recall` tool;
- The routing function `should_continue` is the single "continue/stop" decision point: `has_tool_calls && step < max_steps → tools`, otherwise `END`;
- `max_steps` is a hard cap, so the model calling tools forever won't loop (logs a warning);
- Supports an `emit` event callback: on node execution it emits `node_enter` (agent / tools / human_gate node started), `route` (conditional-edge routing decision: to + reason + step/max_steps), `thought / tool_call / tool_result` (business process) events; the task workbench uses these to stream the loop process live to the frontend (SSE) and render a mini LangGraph topology in the subtask card (node highlight + step count).

### Human-in-the-loop (`human_gate`, inside the Loop)

- Tools flagged `require_approval=True` (`write_file`, `fetch` by default) get intercepted by a `human_gate` node before execution: the node calls LangGraph's native `interrupt()` to suspend the graph and throws the pending tool calls (id/name/args) to the caller; the caller resumes with `Command(resume=...)` from the breakpoint and the graph continues with the same `thread_id`;
- On resume, `human_gate` writes the **human decision** back into a new state field `approvals: {call_id → {action, reason?, args?}}`; the `tools` node checks it on execution: `deny` means don't execute, instead inject a `ToolMessage("User denied: …")` so the model changes its mind (keeping tool_call_id pairing, no OpenAI-compat error); `edit` executes with the edited args; others execute normally;
- Decision forms: bare string `"approve"`/`"deny"` (applies to all pending calls), or per-call `[{id, action, reason?, args?}]`; calls not covered default conservatively to `deny`;
- Chained approval: if a later step again produces a call needing approval, `interrupt()` **suspends again**, `run()` returns empty string, `pending_approval` updates, and the caller loops until it gets the final reply;
- Tests can drive this: `tests/test_approval.py` uses FakeRouter + a real MemorySaver to cover the full path of "not suspended / suspend payload / approve / deny / edit / chained / mixed batch".

### Harness (`harness.py`)

- One class assembles all dependencies, `run()` / `ask()` are the only entry points, `__enter__/__exit__` auto-release the SQLite connection;
- All model routing goes through LiteLLM: switching models is just changing the `LLM_MODEL` string — OpenAI/Anthropic/DeepSeek/any compatible endpoint works;
- The tool registry is the "single execution channel": register with `@h.register_tool`, schema auto-derived from the function signature, tool execution errors injected back as text for the model to retry;
- `run()` first tries the Fast Path / Codegen short-circuit; only if both miss does it enter the LangGraph loop; after each call, `last_trace` records this round's `tool_call / tool_result` events for UI display.

### Tasks (`tasks.py` + `roles.py`) — multi-agent orchestration

```
Planner (break goal into JSON subtask plan)
   → Specialist × N (each subtask runs a tool loop, reusing build_loop)
   → Evaluator (strict acceptance: passed/score/feedback/missing)
   → pass → Reporter (compile deliverable markdown) → task_end
   → fail → re_plan (replan with feedback, at most 1 round) → re-execute
```

- Deterministic goals (arithmetic / time / conversion…) are intercepted first by Fast Path / Codegen, producing a complete task tree directly (labeled "computed in code, zero model calls"), never going through Planner/Evaluator;
- The three roles are independent LLM calls, outputting structured JSON (fault-tolerant parsing: strip markdown fence, tolerate trailing commas, retry on failure);
- Specialists reuse the same LangGraph loop, with the system prompt swapped to the subtask instruction + context;
- **Subtasks run parallel by default (`TaskRunner(..., parallel=4)`)**: planned subtasks fan out concurrently via a thread pool (routing / long-term memory / tool registry are all thread-safe), all `subtask_start` broadcast upfront — the frontend sees the whole batch of "running" cards at once, each Specialist's `subtask_done` on completion, results gathered by index to keep Reporter order stable; `parallel=1` falls back to serial (preserving the "prior results as context" semantics). **Each subtask also runs the Fast Path first**: when the instruction is a deterministic query (e.g. "compute 2+3") it produces the result in code directly (`subtask_done` carries a `fast` flag, zero model calls), skipping the Specialist loop entirely.
- **Each task gets its own sandbox**: `TaskRunner` creates a `<sandbox>/tasks/<task_id>/` working directory per task, and fs tools (read/write/list) are re-bound to that directory — parallel subtasks and sequential tasks **cannot see each other's files**, eliminating pollution from old files/old data (e.g. a rate task reading a stale `fx_parsed.json` causing inconsistency). Planner/Specialist prompts embed a hard rule: **subtasks must be self-contained, must not depend on other subtasks' artifacts, and must fetch any needed data within the subtask** — the Planner merges dependency chains into a single subtask, so parallel mode is safe.
- **Task mode does not enable the approval gate by default**: `build_loop` is called without `needs_approval`, so Specialist tool calls (including `write_file`/`fetch`) execute directly without human confirmation — background tasks have no human to approve in real time, and the task pipeline doesn't depend on human confirmation for side-effecting tools; the approval gate is only mounted on **synchronous interactive** channels like chat/terminal.
- Every inner step event (`task_start / plan / subtask_start / node_enter / route / thought / tool_call / tool_result / subtask_done / evaluation / re_plan / task_end`) enters the queue in real time and is pushed to the frontend via SSE at `/api/tasks/{id}/stream`; events carry a `subtask` tag for the frontend to render the task tree;
- Task records keep all events, so late subscribers still get the full history; `max_steps` is a backstop against runaway.
- **Successful tasks persist their operation log**: on completion (done), the full event stream is written to `data/task_history.db` (SQLite, table `task_history`, keeps the last 500; same stdlib `sqlite3` + lock as long-term memory, thread-safe); after restart, `GET /api/tasks` still lists them and `GET /api/tasks/{id}` can replay every step; failed tasks are not persisted. The top "History" tab lists them newest-first and replays the full task tree. Storage location can be changed via `TaskRunner(..., history_path=...)`.
- **Token usage recorded throughout**: `LiteLLMRouter` extracts the provider's `usage` on each call (prompt/completion/total tokens, thread-safe cumulative); the task carries this run's total cost in the `task_end`/`error` event (Planner+Specialists+Evaluator+Reporter+Codegen all counted), persisted with history; chat `/api/chat` (and `/api/chat/approve`) responses carry this conversation's cost. The frontend shows "⚡ N tokens" under the task deliverable card and chat replies.
- **Task results can be explicitly summarized into long-term memory**: the task tab's deliverable area has "💾 save this result to long-term memory" — after a task completes, click it, enter a memory key, and the deliverable content (≤2000 chars) is written to `scope=default` long-term memory, visible/deletable in the sidebar immediately. This is an **explicit** write (distinct from a Specialist auto-writing during a task — task mode disables the `remember` tool to prevent intermediate artifacts from polluting memory).

### Fast Path (`fastpath.py`) — deterministic fast path

- Coverage: arithmetic expressions (incl. Chinese "23 加 45", comparison intent "which is bigger"), current time, number stats (max/min/avg/sum/sort), unit conversion (Celsius↔Fahrenheit, km↔miles, kg↔lb, hours↔minutes…), date math (tomorrow/yesterday/N days later, days between two dates), base conversion (binary/octal/hex), character stats, sandbox file listing and reading;
- Safety: arithmetic uses an AST-whitelist evaluator (only numbers + binary/unary ops), arbitrary code is never executed;
- Fixed order: built-in matchers → promoted detectors (`generated_detectors.py`) → runtime plugins (`data/fastpath_plugins/`).

### Codegen (`codegen.py`) — let the model write its own fast path

- On a fast-path miss, the harness asks the model if it can be solved deterministically with a pure Python function; if so it generates `detect(text) -> str | None`;

- Generated code goes through triple protection: AST-whitelist validation (only `re`/`math` imports allowed, no dunder, no class def, no eval/exec, no open, no network) → execution in a restricted-builtins namespace (no I/O) → 3-second hard timeout (thread pool + don't join a runaway worker);
- On success, persisted by source hash to `data/fastpath_plugins/gen_<sha10>.py` (same source auto-dedupes), hit directly next time, zero model;
- Plugin management: the Web UI can view source and delete; selecting several and "promoting" merges them into `src/resolve_harness/generated_detectors.py` (each function keeps its trigger comment, overall structure is code-generated, function body hand-editable) as built-in detectors, and the runtime copy is removed immediately.

### Memory (`memory/`)

- **Short-term**: bounded FIFO session transcript (default 40 entries), `as_list()` outputs a clean `{role, content}` sequence, trims the oldest when over cap;
- **Long-term**: SQLite key-value facts (stdlib `sqlite3`, zero deps), supports scope partitioning (isolate by user/session), `remember/recall/search/forget`;
- Long-term memory is exposed to the agent via built-in tools: `remember` (store), `recall` (fetch), `list_memories` (list keys), so the agent itself can "remember" cross-session info.

### Built-in Tools

**Chat mode exposes only a slim subset** (`get_current_time` / `remember` / `recall` / `fetch`) — time, long-term memory, networking, enough for daily Q&A; arithmetic is answered instantly by Fast Path, file tools are out of chat scope. Task mode's Specialists have the full tool set (see below).

| Tool | Description | Mode |
|---|---|---|
| `get_current_time` | current local time (ISO 8601) | chat + task |
| `remember` / `recall` | long-term memory read/write (chat: both; task: **read-only** — can `recall` snapshot, no write to prevent pollution) | chat + task (read) |
| `list_memories` | list memory keys | registered in chat registry, not exposed in chat loop |
| `read_file` / `write_file` / `list_files` | sandbox files (confined within `data/sandbox/`, prevents path traversal), letting the agent actually produce files | task (per-task isolated sandbox dir `tasks/<task_id>/`) |
| `fetch` | web fetch (http/https only, 8s timeout, 20k char truncation, failure returns text) — the only network channel; Fast Path and codegen stay offline | chat + task |

> **No arithmetic tool**: pure math queries ("compute 2+3", "12×34") are evaluated directly by **Fast Path** code, zero model calls; simple calculations inside a task subtask are done by the Specialist itself (for complex math, let the Planner split it into an independent compute subtask).

> ⚠️ **`fetch` is flagged `require_approval=True` by default** (in chat mode `write_file` isn't in the chat tool set, so the gate only hangs on `fetch`): in synchronous channels like chat/terminal, its call goes through the human approval gate first (suspend → approve/reject/edit args → resume); Task mode doesn't mount the gate and executes directly. Custom tools use `@h.register_tool(require_approval=True)` to join the same gate.

Custom tools: `@h.register_tool(description=...)`, parameter schema auto-derived from type annotations (both chat and task registries include it after registration).

## Extending: register your own tools

```python
from resolve_harness import Harness

h = Harness()

@h.register_tool(description="Get the current weather for a city")
def get_weather(city: str) -> str:
    ...  # your implementation

print(h.run("How's the weather in Shanghai today?"))
```

The tool parameter schema is auto-derived from type annotations (`str/int/float/bool` + required detection); pass `parameters=` to the registrar for finer control.

## Configuration

All via environment variables / `.env` (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `LLM_MODEL` | `openai/gpt-4o-mini` | LiteLLM model string |
| `LLM_API_BASE` | empty | OpenAI-compatible endpoint (gateway / vLLM / Ollama) |
| `LLM_API_KEY` | empty | can also use each provider's `*_API_KEY` |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | `0.7` / `2048` | sampling params |
| `LLM_MAX_STEPS` | `10` | loop cap |
| `LLM_SYSTEM_PROMPT` | built-in English prompt | custom persona / system prompt |
| `HARNESS_VERBOSE` | `0` | `1` prints each step's tool call |
| `HARNESS_LOG_LEVEL` | empty | finer log level (DEBUG/INFO/…) |

## API Reference (FastAPI, `api.py`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/health`, `/api/state` | service info / session + memory snapshot |
| GET | `/api/chat/history` | recent chat turns (per-turn token cost persisted: input/output/total) |
| POST | `/api/chat`, `/api/reset` | one chat turn (returns reply + trace + status + usage; on approval-needed tool returns `status:"pending_approval"` + `pending` + `thread_id`) / clear short-term memory |
| POST | `/api/chat/approve` | resume a chat suspended by the approval gate: `body {thread_id, decisions}`; same shape as `/api/chat`, may be `pending_approval` again (chained) |
| GET / POST / DELETE | `/api/memories` | long-term memory: list / write / delete (by key+scope) |
| POST / GET | `/api/tasks` | start task (returns task_id) / task list |
| GET | `/api/tasks/{id}`, `/api/tasks/{id}/stream` | task snapshot / SSE event stream |
| GET / DELETE | `/api/plugins` | plugin list / delete plugin |
| POST | `/api/plugins/promote` | promote selected plugins into `generated_detectors.py` |
| GET / PUT / DELETE | `/api/sandbox`, `/api/sandbox/content`, `/api/sandbox/file` | sandbox files: list (with kind classification) / read-write / delete / clear |
| GET | `/api/sandbox/raw?path=` | raw bytes + correct Content-Type (image / PDF / HTML inline preview) |
| GET / POST | `/api/examples`, `/api/examples/regenerate` | built-in examples / ask model to regenerate a batch |
| POST | `/api/examples/delete` | delete (tombstone) one example, built-in or generated, persists |

## Project Structure

```
src/resolve_harness/
  config.py        Settings (.env → dataclass)
  llm.py           LiteLLMRouter (unified routing + retry + tool_calls parsing)
  harness.py       Harness (public API + fast path/codegen short-circuit + last_trace)
  fastpath.py      deterministic fast path (arithmetic/time/stats/convert/date/base/text stats/sandbox)
  codegen.py        runtime code generation (model writes its own detector: validate → sandbox exec → persist → promote)
  generated_detectors.py  promoted detectors (merged by the plugin UI, function body hand-editable)
  examples.py      task workbench examples (built-in + ask model to regenerate executable examples)
  tasks.py         TaskRunner: multi-agent orchestration (Planner/Specialist/Evaluator/Reporter)
  roles.py         Planner + Evaluator roles (structured JSON output + fault-tolerant parsing)
  api.py           FastAPI layer (/api/chat, /api/tasks, /api/plugins, /api/sandbox + SSE)
  memory/          short + long-term memory (long-term SQLite thread-safe)
  tools/           ToolRegistry + built-in tools (incl. sandbox file tool fs.py)
  graph/           AgentState + build_loop (LangGraph graph, supports emit event callback)
web/               Vite + React + TS frontend (task/chat/plugin/sandbox four tabs, vite proxy /api → :8000)
examples/          chat.py (REPL) / tool_demo.py (script demo) / tasks.md (PSE demo example goals)
tests/             memory / tools / loop / api / tasks / fastpath / codegen / examples offline tests
```

## License

MIT
