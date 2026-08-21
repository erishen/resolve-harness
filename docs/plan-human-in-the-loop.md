# Plan: LangGraph human-in-the-loop 审批门

## 目标

在 agent 循环里加入人工审批：**被标记为 `require_approval` 的工具调用**（默认 `write_file`、`fetch`）在执行前用 LangGraph 原生 `interrupt()` 打断，等待人工「批准 / 拒绝 / 改参数」后用 `Command(resume=...)` 从断点恢复。CLI 终端和 Web 聊天 Tab 都能审批。

## 图结构变化

```
START -> agent -> should_continue
                    ├─ 有 tool_calls 且需审批 ──> human_gate -> tools -> agent
                    ├─ 有 tool_calls            ──────────────> tools -> agent
                    └─ 无 tool_calls / 超步数 ──> END

human_gate: interrupt({"tool_calls": [...]})   # 首次执行抛 GraphInterrupt
            invoke 返回 __interrupt__，caller 决策后:
            graph.invoke(Command(resume=decisions), 同一 thread_id)
            # human_gate 重跑，interrupt() 返回 decisions，写入 state.approvals
```

- 恢复后 `human_gate` 把决策写入新 state 字段 `approvals: dict[call_id -> decision]`；
- `tools_node` 执行时查 `approvals`：`deny` 的调用不执行，改为注入 `ToolMessage("User denied: 原因")`（保证 tool_call_id 配对，OpenAI 兼容 API 不报错）；`edit` 的用改后的 args 执行；其余正常执行。

## 改动清单（TDD：每步先写测试）

### 1. `tools/registry.py` — 审批标记
- `Tool` 增加 `require_approval: bool = False`；`register(..., require_approval=False)` 透传；
- `ToolRegistry.needs_approval(name) -> bool`。
- `tools/builtin.py`：`write_file`、`fetch` 注册时加 `require_approval=True`。

### 2. `graph/state.py` — 新字段
- `approvals: dict[str, Any]`（默认 `{}`；普通字段无 reducer，整体替换）。

### 3. `graph/loop.py` — human_gate 节点
- `build_loop(..., needs_approval: Callable[[str], bool] | None = None)`：
  - 传入时编译带 `checkpointer=MemorySaver()` 的图，并新增 `human_gate` 节点 + 路由（`should_continue` 三分支）；
  - 不传时行为与现在完全一致（无 checkpointer、无 gate）——`tasks.py` 的 Specialist 循环不传，天然不受影响（后台任务无人审批，README 注明）。
- `emit` 新事件：`approval_request`（进入 interrupt 前）、`approval_result`（收到决策后）。
- interrupt 返回值（payload）= `[{"id", "name", "args"}, ...]`（仅含需审批的调用）；resume 值 = 决策列表 `[{"id", "action": "approve"|"deny"|"edit", "reason"?, "args"?}, ...]`。

### 4. `harness.py` — 挂起 / 恢复
- 持有 `MemorySaver`；`run()` 每轮生成新 `thread_id`（短期记忆仍是历史唯一来源，避免 checkpointer 重复累积）；
- `invoke` 结果含 `__interrupt__` 时：设置 `self.pending_approval = {"thread_id", "tool_calls"}`，返回空串（不写 assistant 消息）；
- 新方法 `resolve_approval(decisions) -> str`：`invoke(Command(resume=decisions), 同 thread_id)`，复用 run() 的收尾逻辑（提取 reply / trace / steps / 写短期记忆）；恢复后若再次 interrupt 则更新 `pending_approval`，由调用方循环处理；
- `run()` 开头若还有未决 pending：自动全部 deny 把上一轮收尾，保证状态一致。

### 5. `api.py` — 挂起/审批端点
- `POST /api/chat` 响应增加 `status: "done" | "pending_approval"`、`pending`、`thread_id`；
- 新增 `POST /api/chat/approve`：body `{thread_id, decisions: [{id, action, reason?, args?}]}`，返回与 chat 相同结构（可能再次 pending——链式审批）。

### 6. `cli.py` — 终端审批
- `run()` 后若 `pending_approval`：逐个打印工具名 + 参数，提示 `[y]批准 / [n]拒绝 / [e]改参数(JSON)`，收集决策调 `resolve_approval()`，循环直到拿到最终回复。

### 7. Web（聊天 Tab）
- `types.ts`：`ApprovalRequest` / `ApprovalDecision` 类型；chat 响应类型扩展；
- `api.ts`：`approve(threadId, decisions)`；
- `ChatPanel.tsx`：响应为 pending 时在消息流渲染审批卡片（工具名、参数 JSON、批准/拒绝按钮、可展开编辑参数），提交后调 approve，结果可能是新审批卡或最终回复；
- `index.css`：卡片样式。

### 8. 测试（新增 `tests/test_approval.py` + 扩展）
- 未标记工具：不触发 gate，行为不变（回归保障）；
- 标记工具：`__interrupt__` 出现且 payload 正确；
- resume approve / deny / edit 三条路径（deny 时 ToolMessage 内容与配对正确）；
- 恢复后二次 interrupt（链式）；
- registry 标记与 `needs_approval`；
- harness：pending 状态、auto-deny、resolve 收尾；
- api：chat 返回 pending_approval、approve 后拿到最终回复（沿用现有 fake 注入模式）。

### 9. README 更新
架构图加 human_gate、Loop 一节加 HITL 说明、API 表加 `/api/chat/approve`、注明任务模式不启用审批门及原因。

## 顺序

registry → state/loop（核心）→ harness → api → cli → web → README，每步跑 `uv run pytest` 保持全绿。
