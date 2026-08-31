# resolve_harness

一个极简的 Python AI Agent 项目骨架：**LangGraph** 编排循环（Loop）、**LiteLLM** 统一模型路由（Harness）、**分层记忆**（Memory）、**确定性快路径**（Fast Path：能算的绝不让模型算）。用 `uv` 管理依赖与运行环境。

设计目标不是"又一个 agent 框架"，而是把 agent 的关键机制**拆开、讲清、可替换**：

| 支柱 | 位置 | 职责 |
|---|---|---|
| **Loop** | `src/resolve_harness/graph/` | LangGraph StateGraph：`agent → tools → agent…`，带 `max_steps` 防失控 |
| **Harness** | `src/resolve_harness/harness.py` | 唯一入口：组装配置、模型路由、工具注册表、记忆、循环 |
| **Memory** | `src/resolve_harness/memory/` | 短期（会话转录）+ 长期（SQLite 事实，经工具读写） |
| **Fast Path** | `src/resolve_harness/fastpath.py` + `codegen.py` | 确定性查询纯代码解决：命中即零模型；未命中时让模型生成检测器并持久化复用 |

## ✨ 核心亮点：Fast-path 插件运行时（模型自写 · 零模型复用 · 可晋升进源码）

大多数"任务"其实是确定性的——算个账、转个进制、查个汇率——让 LLM 反复跑既慢又贵还易错。resolve_harness 用三层快路径把这部分彻底拿掉，且**整个过程对 UI / 记忆 / 编排循环完全透明**（任务树照常展示，并标注"零模型"）：

1. **内置匹配器**（`fastpath.py`）：算术 / 时间 / 单位换算 / 日期 / 进制 / 文本统计 / 指数行情……命中即纯代码直算，**零模型调用**。
2. **Codegen（让模型写自己的快路径）**：未命中时，harness 问模型"这问题能否用纯 Python 函数确定性解决"；能则生成 `detect(text) -> str | None`，经 AST 白名单沙箱校验后**持久化**到 `data/fastpath_plugins/`，本次立即复用，下次同题直接命中。
3. **晋升（promote）**：在「插件」Tab 选中表现好的检测器，「晋升」即合并进 `src/resolve_harness/generated_detectors.py`，成为随源码提交的内置检测器，运行时副本随即清理。

一条龙：**开发 → 持久化 → 复用 → 转正**。效果：第一次问"2024 是闰年吗"模型现写检测器；之后再问同类问题，连模型都不用调。详见下方「Fast Path」与「Codegen」两节，以及「插件」Tab 的晋升说明。

## 快速开始

```bash
cd work/harness/resolve_harness
cp .env.example .env          # 填 LLM_MODEL / LLM_API_KEY
uv sync                        # 安装依赖（国内自动走清华镜像）
uv run resolve_harness-chat         # 交互式对话（终端）
```

不带真实 key 也能跑通全链路：`uv run pytest` 用 FakeRouter 驱动循环，不碰网络。

```bash
uv run pytest                 # 离线单元测试（memory / tools / loop / api / tasks / fastpath / codegen）
uv run python examples/tool_demo.py   # 脚本演示：时间/计算/记忆
```

### Web UI（Vite + React）

一键启动前后端（推荐）：

```bash
make dev
```

`make dev` 会：① 先清理占用 `:8000` / `:5173` 的残留进程 → ② 启动 FastAPI 后端并等健康检查通过 → ③ 再启动 Vite 前端 → 打开 http://localhost:5173。Ctrl-C 一次停止两者。

也可以分两个终端手动起：

```bash
# 终端 1：FastAPI 后端（http://127.0.0.1:8000，交互文档在 /docs）
make api

# 终端 2：Vite 前端（http://localhost:5173）
cd web && pnpm install        # 首次
make web-dev
```

打开 http://localhost:5173 即可使用，共**八个 Tab**（聊天为默认，第一个）：

- **任务**（多 Agent 工作台）：顶部是示例卡片（内置 + 「重新生成」按钮让模型生成一批新的、可执行的示例），内置示例覆盖计算 / 文档 / 代码 / 联网抓取（海外 JD 列表、A 股实时行情、美元汇率，均走 fetch 工具）等类型；卡片可单击填入、双击直接运行，也可 hover 删除——删除会持久化（内置/生成都能删，刷新后不再出现），且已删示例会作为「负面清单」注入重新生成的 prompt，避免模型再产出类似任务；输入一个目标后，**Planner 拆解子任务 → Specialist 逐个执行（工具循环）→ Evaluator 验收**（不达标自动打回重规划，最多 1 轮）→ **Reporter 汇成交付**。每一步（计划 / 子任务进度 / 思考 / 工具调用 / 验收结论）通过 SSE 实时流式显示为任务树，交付支持 markdown。若目标是确定性查询（如「计算 2+3」），会直接走 Fast Path 秒回，任务树依然完整展示并标注「零模型」。
- **聊天**：一问一答，工具调用以回复下方的小标签展示；确定性查询同样走 Fast Path，无需等模型。
- **历史**：已持久化的成功任务（`data/task_history.db`）按时间倒序列出，点击任意一条回看完整任务树（计划 / 子任务 / LangGraph 循环图 / 工具调用 / 产出文件 / 交付），同样支持「存入长期记忆」。
- **插件**：管理运行时生成的 fast-path 插件——查看源码、单个删除，或「晋升」把表现好的插件合并进 `src/resolve_harness/generated_detectors.py` 成为内置检测器（随源码提交）。
- **工具**：列出项目提供的全部工具（`GET /api/tools`）——每个工具的名称、描述、参数（含必填）、审批标记，以及它在哪些模式可用（聊天 / 任务），支持按模式过滤。
- **Agent**：任务流水线的四个角色（Planner / Specialist / Evaluator / Reporter）卡片 + Orchestrator 流程 SVG 图（含 Fast Path / codegen 短路、达标分支与失败重规划回环）。
- **沙箱**：浏览 `data/sandbox/` 下的文件（按修改时间倒序，最新在最上），支持单个删除、一键清空；预览按类型渲染——markdown 渲染成文档、CSV/TSV 渲染成表格、HTML 在 iframe 中展示、图片直接显示、PDF 内嵌查看、JSON 自动格式化 + 语法高亮、代码（py/js/ts/go/rust…）语法高亮，文本类均可切回源码编辑，纯文本保持源码视图。
- **设置**：运行时配置——模型库（多个命名模型，每个含 Base URL / 模型名 / API Key 环境变量名，key 本体放 .env 不落盘）、全局默认模型（覆盖 .env `LLM_MODEL`，可清除回退）、四个 Agent（Planner/Specialist/Evaluator/Reporter）各自选择模型库中的模型（留空跟随默认）、Specialist 循环步数上限 / 并行子任务数 N / 失败重试轮数。所有修改即时生效（下一任务起）并持久化到 `data/config.json`（`GET/PUT /api/config`）。

右侧栏实时列出长期记忆（可删除），顶部「清空会话」重置短期记忆。Vite dev 已配置 `/api` 代理到后端，无需处理 CORS。

## 架构

```
┌────────────────────────────── Harness ─────────────────────────────┐
│                                                                     │
│  Settings  ──►  LiteLLMRouter  ──►  任意 provider / 兼容端点        │
│  (.env)                                                            │
│                                                                     │
│  Fast Path  命中确定性查询（算术/时间/换算/…）→ 直接回答，零模型     │
│  Codegen    未命中时让模型写检测器，校验后持久化 → 下次零模型        │
│                                                                     │
│  ShortTermMemory  ◄──  Loop (LangGraph StateGraph)  ──►  ToolRegistry│
│  (会话转录, 有界)      agent ──► tools ──► agent ──► END            │
│                       agent ──(需审批工具)──► human_gate            │
│                       (interrupt 挂起 ⇄ Command(resume) 恢复)        │
│                       max_steps 硬上限                              │
│  LongTermMemory  ◄──────────────────────────────────────┘          │
│  (SQLite 事实, 跨会话)     ↑ remember / recall / list_memories 工具  │
└────────────────────────────────────────────────────────────────────┘
```

一次 `harness.run(text)` 的旅程：

1. **Fast Path**：先用纯代码尝试——算术、当前时间、统计、单位换算、日期推算、进制转换、字符统计、沙箱文件读取。命中即返回，零模型调用；
2. **Codegen**：未命中时，问模型"这个问题能否用纯 Python 函数确定性解决"；能则生成检测器，沙箱校验通过后运行，成功后持久化为插件（下次同样问题零模型）；
3. **循环**：仍未解决 → 进入 LangGraph loop。用户消息连同历史转录作为初始 state；`agent` 节点产出回复或 `tool_calls`，有工具调用且 `step < max_steps` 时路由到 `tools` 节点执行，结果回注后回到 `agent` 继续推理；
4. 最终回复写回短期记忆；agent 通过 `remember` 写入的事实进入长期记忆。

## 核心设计

### Loop（`graph/loop.py`）

- 纯手写 StateGraph，不用 prebuilt `ToolNode`——循环的每个决策点都可见、可改；
- `AgentState` 用 LangGraph 的 `add_messages` reducer 做消息累积与工具结果配对；
- **发送给 LLM 前做历史裁剪**（`_trim_history`，上限 `MAX_LLM_MESSAGES=24` 条）：多轮对话的历史每轮都要全量重发，超出部分裁掉最老的轮次以控制 token 消耗；裁剪点绝不会落在 `ToolMessage` 上（会自动前扩包含配对的 assistant 工具调用，保证 OpenAI 兼容），长期事实仍可经 `recall` 工具取回；
- 路由函数 `should_continue` 是唯一的"继续/停止"决策点：`has_tool_calls && step < max_steps → tools`，否则 `END`；
- `max_steps` 是硬上限，模型一直调工具也不会死循环（日志会告警）；
- 支持 `emit` 事件回调：节点执行时发出 `node_enter`（agent / tools / human_gate 节点开始运行）、`route`（条件边路由决策：to + reason + step/max_steps）、`thought / tool_call / tool_result`（业务过程）事件，任务工作台借此把循环过程实时流给前端（SSE），并在子任务卡片里渲染迷你 LangGraph 拓扑图（节点高亮 + step 计数）。

### Human-in-the-loop（`human_gate`，位于 Loop 内部）

- 标记 `require_approval=True` 的工具（`write_file`、`fetch` 默认开启），在执行前用一个 `human_gate` 节点拦一道：节点调用 LangGraph 原生 `interrupt()` 把图挂起，把待审批的工具调用（id/name/args）抛给调用方；调用方决策后带着 `Command(resume=...)` 从断点恢复，图复用同一 `thread_id` 继续跑；
- 恢复时 `human_gate` 把**人工决策**写回新 state 字段 `approvals: {call_id → {action, reason?, args?}}`；`tools` 节点执行时查它：`deny` 的不执行、改为注入一条 `ToolMessage("User denied: …")` 让模型改主意（保持 tool_call_id 配对，OpenAI 兼容 API 不报错）；`edit` 用改后的 args 执行；其余正常执行；
- 决策形式：裸字符串 `"approve"`/`"deny"`（对所有待审批调用生效），或逐条 `[{id, action, reason?, args?}]`；未被覆盖的调用保守按 `deny` 处理；
- 链式审批：恢复后若后续步骤又出现需审批的调用，`interrupt()` 会**再次**挂起，`run()` 返回空串、`pending_approval` 更新，调用方循环处理直到拿到最终回复；
- 测试可驱动：`tests/test_approval.py` 用 FakeRouter + 真实 MemorySaver 覆盖「未挂起/挂起 payload/approve/deny/edit/链式/混合批次」全路径。

### Harness（`harness.py`）

- 一个类组装所有依赖，`run()` / `ask()` 是唯一入口，`__enter__/__exit__` 自动释放 SQLite 连接；
- 模型路由全部走 LiteLLM：换模型只改 `LLM_MODEL` 一个字符串，OpenAI/Anthropic/DeepSeek/任意兼容端点通用；
- 工具注册表是"唯一执行通道"：`@h.register_tool` 注册，schema 自动从函数签名推导，工具执行错误会以文本回注给模型重试；
- `run()` 先尝试 Fast Path / Codegen 短路，两者都未命中才进 LangGraph 循环；每次调用后 `last_trace` 记录本轮的 `tool_call / tool_result` 事件供 UI 展示。

### Tasks（`tasks.py` + `roles.py`）— 多 Agent 编排

```
Planner(拆解目标为 JSON 子任务计划)
   → Specialist × N(每个子任务跑一个工具循环,复用 build_loop)
   → Evaluator(严格验收:passed/score/feedback/missing)
   → 通过 → Reporter(汇成交付 markdown) → task_end
   → 未通过 → re_plan(带反馈重规划,最多 1 轮) → 重新执行
```

- 确定性目标（算术 / 时间 / 换算…）会先被 Fast Path / Codegen 截获，直接产出完整任务树（标注"代码直接计算，零模型调用"），完全不经过 Planner/Evaluator；
- 三个角色都是独立 LLM 调用，输出结构化 JSON（容错解析：剥 markdown fence、容忍尾逗号、失败重试）；
- Specialist 复用同一个 LangGraph loop，system prompt 换成子任务指令 + 上下文；
- **子任务默认并行（`TaskRunner(..., parallel=4)`）**：计划的子任务用线程池 fan-out 并发执行（路由/长期记忆/工具注册表均线程安全），所有 `subtask_start` 提前广播——前端一次看到整批「执行中」卡片，各 Specialist 完成后各自 `subtask_done`，结果按 index 归集保证 Reporter 顺序稳定；`parallel=1` 回退串行（此时保留「前序结果作上下文」语义）。**每个子任务执行前也会先跑 Fast Path**：指令是确定性查询（如「计算 2+3」）时直接代码出结果（`subtask_done` 带 `fast` 标记、零模型调用），完全跳过 Specialist 循环。
- **每个任务拥有独立沙箱**：`TaskRunner` 为每个任务创建 `<sandbox>/tasks/<task_id>/` 工作目录，fs 工具（read/write/list）重新绑定到该目录——并行子任务与先后任务**互不可见对方的文件**，杜绝旧文件/旧数据污染（如汇率任务读到历史残留的 `fx_parsed.json` 导致数据不一致）。Planner/Specialist 的 prompt 已写入硬规则：**子任务必须自足，禁止依赖其他子任务产物，所需数据在本子任务内自行获取**——Planner 会把依赖链合并进单个子任务，并行模式因此安全。
- **任务模式默认不启用审批门**：`build_loop` 不传 `needs_approval`，所以 Specialist 的工具调用（含 `write_file`/`fetch`）不经人工确认直接执行——后台任务无人实时审批，且任务流水线对副作用工具不依赖人工确认；聊天/终端等**同步交互**通道才挂载审批门。
- 每个 inner step 事件（`task_start / plan / subtask_start / node_enter / route / thought / tool_call / tool_result / subtask_done / evaluation / re_plan / task_end`）实时进入队列，`/api/tasks/{id}/stream` 以 SSE 推给前端，事件带 `subtask` 标签便于前端渲染任务树；
- 任务记录保留全部事件，晚订阅者也能拿到完整历史；`max_steps` 兜底防止失控。
- **成功任务的操作记录会持久化**：任务完成（done）时把完整事件流写入 `data/task_history.db`（SQLite，表 `task_history`，保留最近 500 条；与长期记忆同款 stdlib `sqlite3` + 锁，线程安全），重启后 `GET /api/tasks` 仍能列出、`GET /api/tasks/{id}` 可回看每一步；失败任务不落盘。顶部「历史」Tab 按时间倒序列出并回放完整任务树。可通过 `TaskRunner(..., history_path=...)` 换存储位置。
- **Token 消耗全程记录**：`LiteLLMRouter` 每次调用提取 provider 的 `usage`（prompt/completion/total tokens，线程安全累计）；任务在 `task_end`/`error` 事件里带本次任务的总消耗（Planner+Specialists+Evaluator+Reporter+Codegen 全部计入），随历史落库；聊天 `/api/chat`（及 `/api/chat/approve`）响应带本次对话消耗。前端在任务交付卡片、聊天回复下方显示「⚡ N tokens」。
- **任务结果可显式汇总进长期记忆**：任务 Tab 的交付区有「💾 把本次结果存入长期记忆」——任务完成后点一下，输入一个记忆 key，交付内容（≤2000 字）即写入 `scope=default` 长期记忆，侧栏立即可见/可删。这是**显式**写入（区别于 Specialist 在任务中自动写——任务模式已禁用 `remember` 工具，杜绝中间产物污染）。

### Fast Path（`fastpath.py`）— 确定性快路径

- 覆盖：算术表达式（含中文「23 加 45」、比较意图「哪个更大」）、当前时间、数字统计（最大/最小/平均/总和/排序）、单位换算（摄氏↔华氏、公里↔英里、千克↔磅、小时↔分钟…）、日期推算（明天/昨天/N 天后、两日期相差几天）、进制转换（二/八/十六进制）、字符统计、沙箱文件列表与读取；
- 安全：算术用 AST 白名单求值（仅数字 + 二元/一元运算），任意代码永不执行；
- 顺序固定：内置匹配器 → 已晋升检测器（`generated_detectors.py`）→ 运行时插件（`data/fastpath_plugins/`）。

### Codegen（`codegen.py`）— 让模型写自己的快路径

- 快路径未命中时，harness 问模型能否用纯 Python 函数确定性解决；能则生成 `detect(text) -> str | None`；
- 生成代码经过三重防护：AST 白名单校验（仅允许 `re`/`math` 的 import，禁 dunder、类定义、eval/exec、open、网络等）→ 受限 builtins 命名空间执行（无 I/O）→ 3 秒硬超时（线程池 + 不 join 失控 worker）；
- 成功后按源码哈希持久化为 `data/fastpath_plugins/gen_<sha10>.py`（相同源码自动去重），下次同样问题直接命中、零模型；
- 插件管理：Web UI 可查看源码、删除；选中多个可「晋升」，合并进 `src/resolve_harness/generated_detectors.py`（每个函数保留 trigger 注释，整体结构由代码生成，函数体可手改）成为内置检测器，运行时副本随即移除。

### Memory（`memory/`）

- **短期**：有界 FIFO 会话转录（默认 40 条），`as_list()` 输出纯净 `{role, content}` 序列，超限裁剪最旧；
- **长期**：SQLite 键值事实（stdlib `sqlite3`，零依赖），支持 scope 分区（按用户/会话隔离），`remember/recall/search/forget`；
- 长期记忆通过内置工具暴露给 agent：`remember`（存事实）、`recall`（取事实）、`list_memories`（看键），从而 agent 自己就能"记住"跨会话信息。

### 内置工具

**聊天模式只暴露精简子集**（`get_current_time` / `remember` / `recall` / `fetch`）——时间、长期记忆、联网，够日常问答；算术走 Fast Path 秒回，文件工具不在聊天范围。任务模式的 Specialist 另有完整工具集（见下）。

| 工具 | 说明 | 模式 |
|---|---|---|
| `get_current_time` | 当前本地时间（ISO 8601） | 聊天 + 任务 |
| `remember` / `recall` | 长期记忆读写（聊天：读写都有；任务：**只读**——可 `recall` 快照，禁写防污染） | 聊天 + 任务(读) |
| `list_memories` | 列出记忆 key | 聊天注册表保留、聊天循环不暴露 |
| `read_file` / `write_file` / `list_files` | 沙箱文件（限定在 `data/sandbox/` 内，防目录穿越），让 agent 能真正产出文件 | 任务（每任务独立沙箱目录 `tasks/<task_id>/`） |
| `fetch` | 联网抓取（仅 http/https，8s 超时、2 万字符截断、失败回传文本）——唯一联网通道，Fast Path 与 codegen 保持禁网 | 聊天 + 任务 |

> **没有算术工具**：纯数学查询（「计算 2+3」「12×34」）由 **Fast Path** 代码直接求值、零模型调用；任务子任务内的简单计算由 Specialist 自行完成（复杂运算建议让 Planner 拆成独立计算子任务）。

> ⚠️ **`fetch` 默认标记 `require_approval=True`**（聊天模式下 `write_file` 不在聊天工具集，故审批门只挂在 `fetch` 上）：在聊天/终端等同步通道里，它的调用会先经过人工审批门（挂起 → 批准/拒绝/改参数 → 恢复）；任务（Task）模式不挂载审批门，直接执行。自定义工具用 `@h.register_tool(require_approval=True)` 即可纳入同一道门。

自定义工具：`@h.register_tool(description=...)`，参数 schema 自动从类型注解推导（注册后聊天与任务注册表都会包含）。

## 扩展：注册自己的工具

```python
from resolve_harness import Harness

h = Harness()

@h.register_tool(description="Get the current weather for a city")
def get_weather(city: str) -> str:
    ...  # 你的实现

print(h.run("上海今天天气怎么样？"))
```

工具参数 schema 自动从类型注解推导（`str/int/float/bool` + 必填判断）；需要更细控制时传 `parameters=` 给注册器。

## 配置

全部通过环境变量 / `.env`（见 `.env.example`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_MODEL` | `openai/gpt-4o-mini` | LiteLLM 模型串 |
| `LLM_API_BASE` | 空 | OpenAI 兼容端点（网关 / vLLM / Ollama） |
| `LLM_API_KEY` | 空 | 也可直接用各 provider 的 `*_API_KEY` |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | `0.7` / `2048` | 采样参数 |
| `LLM_MAX_STEPS` | `10` | 循环上限 |
| `LLM_SYSTEM_PROMPT` | 内置英文提示词 | 自定义人设 / system prompt |
| `HARNESS_VERBOSE` | `0` | `1` 时打印每一步的工具调用 |
| `HARNESS_LOG_LEVEL` | 空 | 更细的日志级别（DEBUG/INFO/…） |

## API 一览（FastAPI，`api.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health`、`/api/state` | 服务信息 / 会话 + 记忆快照 |
| GET | `/api/chat/history` | 最近聊天回合（落库的每轮 token 消耗：输入/输出/总） |
| POST | `/api/chat`、`/api/reset` | 一次对话（返回 reply + trace + status + usage；遇需审批工具返回 `status:"pending_approval"` + `pending` + `thread_id`）/ 清空短期记忆 |
| POST | `/api/chat/approve` | 恢复被审批门挂起的对话：`body {thread_id, decisions}`；返回结构与 `/api/chat` 相同，可能再次 `pending_approval`（链式审批） |
| GET / POST / DELETE | `/api/memories` | 长期记忆：列表 / 写入 / 删除（按 key+scope） |
| POST / GET | `/api/tasks` | 启动任务（返回 task_id）/ 任务列表 |
| GET | `/api/tasks/{id}`、`/api/tasks/{id}/stream` | 任务快照 / SSE 事件流 |
| GET / DELETE | `/api/plugins` | 插件列表 / 删除插件 |
| POST | `/api/plugins/promote` | 晋升选中插件到 `generated_detectors.py` |
| GET / PUT / DELETE | `/api/sandbox`、`/api/sandbox/content`、`/api/sandbox/file` | 沙箱文件：列表（含 kind 分类）/ 读写 / 删除 / 清空 |
| GET | `/api/sandbox/raw?path=` | 原始字节 + 正确 Content-Type（图片 / PDF / HTML 内嵌预览） |
| GET / POST | `/api/examples`、`/api/examples/regenerate` | 内置示例 / 让模型重新生成一批示例 |
| POST | `/api/examples/delete` | 删除（墓碑化）一个示例，内置/生成均可，持久生效 |

## 项目结构

```
src/resolve_harness/
  config.py        Settings（.env → dataclass）
  llm.py           LiteLLMRouter（统一路由 + 重试 + tool_calls 解析）
  harness.py       Harness（对外 API + fast path/codegen 短路 + last_trace）
  fastpath.py      确定性快路径（算术/时间/统计/换算/日期/进制/文本统计/沙箱）
  codegen.py       运行时代码生成（模型自写检测器：校验→沙箱执行→持久化→晋升）
  generated_detectors.py  晋升的检测器（插件管理界面合并生成，函数体可手改）
  examples.py      任务工作台示例（内置 + 让模型重新生成可执行示例）
  tasks.py         TaskRunner：多 Agent 编排（Planner/Specialist/Evaluator/Reporter）
  roles.py         Planner + Evaluator 角色（结构化 JSON 输出 + 容错解析）
  api.py           FastAPI 层（/api/chat、/api/tasks、/api/plugins、/api/sandbox + SSE）
  memory/          短期 + 长期记忆（长期 SQLite 线程安全）
  tools/           ToolRegistry + 内置工具（含沙箱文件工具 fs.py）
  graph/           AgentState + build_loop（LangGraph 图，支持 emit 事件回调）
web/               Vite + React + TS 前端（任务/聊天/插件/沙箱 四 Tab，vite proxy /api → :8000）
examples/          chat.py（REPL）/ tool_demo.py（脚本演示）/ tasks.md（PSE 演示示例目标）
tests/             memory / tools / loop / api / tasks / fastpath / codegen / examples 离线测试
```

## 许可证

MIT

## 相关文章
- [能算的绝不调模型：resolve-harness 的确定性 Fast Path 运行时](https://erishen.cn/resolve_harness/)
