# agentpulse

一个极简的 Python AI Agent 项目骨架：**LangGraph** 编排循环（Loop）、**LiteLLM** 统一模型路由（Harness）、**分层记忆**（Memory）。用 `uv` 管理依赖与运行环境。

设计目标不是"又一个 agent 框架"，而是把 agent 的三个关键机制**拆开、讲清、可替换**：

| 支柱 | 位置 | 职责 |
|---|---|---|
| **Loop** | `src/agentpulse/graph/` | LangGraph StateGraph：`agent → tools → agent…`，带 `max_steps` 防失控 |
| **Harness** | `src/agentpulse/harness.py` | 唯一入口：组装配置、模型路由、工具注册表、记忆、循环 |
| **Memory** | `src/agentpulse/memory/` | 短期（会话转录）+ 长期（SQLite 事实，经工具读写） |

## 快速开始

```bash
cd work/harness/agentpulse
cp .env.example .env          # 填 LLM_MODEL / LLM_API_KEY
uv sync                        # 安装依赖（国内自动走清华镜像）
uv run agentpulse-chat         # 交互式对话（终端）
```

不带真实 key 也能跑通全链路：`uv run pytest` 用 FakeRouter 驱动循环，不碰网络。

```bash
uv run pytest                 # 离线单元测试（memory / tools / loop / api）
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

打开 http://localhost:5173 即可使用。界面有**两个模式**：

- **聊天**：一问一答，工具调用以回复下方的小标签展示；
- **任务**（多 Agent 工作台）：输入一个目标，**Planner 拆解子任务 → Specialist 逐个执行（工具循环）→ Evaluator 验收**，不达标自动打回重规划（最多 1 轮）→ Reporter 汇成交付。每一步（计划 / 子任务进度 / 思考 / 工具调用 / 验收结论）通过 SSE 实时流式显示为任务树，交付支持 markdown。

右侧栏实时列出长期记忆（可删除），顶部「清空会话」重置短期记忆。Vite dev 已配置 `/api` 代理到后端，无需处理 CORS。

## 架构

```
┌────────────────────────────── Harness ──────────────────────────────┐
│                                                                     │
│  Settings  ──►  LiteLLMRouter  ──►  任意 provider / 兼容端点         │
│  (.env)                                                             │
│                                                                     │
│  ShortTermMemory  ◄──  Loop (LangGraph StateGraph)  ──►  ToolRegistry│
│  (会话转录, 有界)      │ agent ──► tools ──► agent ──► END            │  (内置 + 自定义)
│                       │ max_steps 硬上限                            │
│  LongTermMemory  ◄────────────────────────────────────────┘         │
│  (SQLite 事实, 跨会话)     ↑ remember / recall / list_memories 工具   │
└─────────────────────────────────────────────────────────────────────┘
```

一次 `harness.run(text)` 的旅程：

1. 用户消息进入 `ShortTermMemory`，连同历史转录一起作为初始 state；
2. `agent` 节点：system prompt + 历史 + 工具 schema → LiteLLM → 可能返回 `tool_calls`；
3. 有 `tool_calls` 且 `step < max_steps` → 路由到 `tools` 节点执行，结果作为 `ToolMessage` 回注；
4. 回到 `agent` 继续推理；无工具调用或触达上限 → 终止；
5. 最终回复写回短期记忆；agent 通过 `remember` 写入的事实进入长期记忆。

## 三个设计

### Loop（`graph/loop.py`）

- 纯手写 StateGraph，不用 prebuilt `ToolNode`——循环的每个决策点都可见、可改；
- `AgentState` 用 LangGraph 的 `add_messages` reducer 做消息累积与工具结果配对；
- 路由函数 `should_continue` 是唯一的"继续/停止"决策点：`has_tool_calls && step < max_steps → tools`，否则 `END`；
- `max_steps` 是硬上限，模型一直调工具也不会死循环（日志会告警）；
- 支持 `emit` 事件回调：节点执行时发出 `thought / tool_call / tool_result` 事件，任务工作台借此把循环过程实时流给前端（SSE）。

### Harness（`harness.py`）

- 一个类组装所有依赖，`run()` / `ask()` 是唯一入口，`__enter__/__exit__` 自动释放 SQLite 连接；
- 模型路由全部走 LiteLLM：换模型只改 `LLM_MODEL` 一个字符串，OpenAI/Anthropic/DeepSeek/任意兼容端点通用；
- 工具注册表是"唯一执行通道"：`@h.register_tool` 注册，schema 自动从函数签名推导，工具执行错误会以文本回注给模型重试。

### Tasks（`tasks.py` + `roles.py`）— 多 Agent 编排

```
Planner(拆解目标为 JSON 子任务计划)
   → Specialist × N(每个子任务跑一个工具循环,复用 build_loop)
   → Evaluator(严格验收:passed/score/feedback/missing)
   → 通过 → Reporter(汇成交付 markdown) → task_end
   → 未通过 → re_plan(带反馈重规划,最多 1 轮) → 重新执行
```

- 三个角色都是独立 LLM 调用，输出结构化 JSON（容错解析：剥 markdown fence、容忍尾逗号、失败重试）；
- Specialist 复用同一个 LangGraph loop，system prompt 换成子任务指令 + 前序子任务结果上下文；
- 每个 inner step 事件（`task_start / plan / subtask_start / thought / tool_call / tool_result / subtask_done / evaluation / re_plan / task_end`）实时进入队列，`/api/tasks/{id}/stream` 以 SSE 推给前端，事件带 `subtask` 标签便于前端渲染任务树；
- 任务记录保留全部事件，晚订阅者也能拿到完整历史；`max_steps` 兜底防止失控。

### Memory（`memory/`）

- **短期**：有界 FIFO 会话转录（默认 40 条），`as_list()` 输出纯净 `{role, content}` 序列，超限裁剪最旧；
- **长期**：SQLite 键值事实（stdlib `sqlite3`，零依赖），支持 scope 分区（按用户/会话隔离），`remember/recall/search/forget`；
- 长期记忆通过内置工具暴露给 agent：`remember`（存事实）、`recall`（取事实）、`list_memories`（看键），从而 agent 自己就能"记住"跨会话信息。

### 内置工具

| 工具 | 说明 |
|---|---|
| `get_current_time` / `add` | 时间 / 算术 |
| `remember` / `recall` / `list_memories` | 长期记忆读写 |
| `read_file` / `write_file` / `list_files` | 沙箱文件（限定在 `data/sandbox/` 内，防目录穿越），让 agent 能真正产出文件 |

自定义工具：`@h.register_tool(description=...)`，参数 schema 自动从类型注解推导。

## 扩展：注册自己的工具

```python
from agentpulse import Harness

h = Harness()

@h.register_tool(description="Fetch a URL and return the text content")
def fetch(url: str) -> str:
    ...  # 你的实现

print(h.run("帮我抓一下 https://example.com 的标题"))
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
| `HARNESS_VERBOSE` | `0` | `1` 时打印每一步的工具调用 |

## 项目结构

```
src/agentpulse/
  config.py        Settings（.env → dataclass）
  llm.py           LiteLLMRouter（统一路由 + 重试 + tool_calls 解析）
  harness.py       Harness（对外 API + last_trace 工具调用追踪）
  tasks.py         TaskRunner：多 Agent 编排（Planner/Specialist/Evaluator/Reporter）
  roles.py         Planner + Evaluator 角色（结构化 JSON 输出 + 容错解析）
  api.py           FastAPI 层（/api/chat、/api/memories、/api/tasks + SSE）
  memory/          短期 + 长期记忆（长期 SQLite 线程安全）
  tools/           ToolRegistry + 内置工具（含沙箱文件工具 fs.py）
  graph/           AgentState + build_loop（LangGraph 图，支持 emit 事件回调）
web/               Vite + React + TS 前端（聊天 + 任务工作台，vite proxy /api → :8000）
examples/          chat.py（REPL）/ tool_demo.py（脚本演示）
tests/             memory / tools / loop / api / tasks 离线测试
```

## 许可证

MIT
