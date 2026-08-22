# resolve_harness 开发 TODO

> 项目待办与规划。完成项请勾选 `[x]` 并保留，便于追溯。
> 维护原则：每条尽量标注落点（相关模块/文件），保持可执行。

## 一、可靠性与兜底（高优先）

- [ ] **泛化「确定性脚本」模式**：把 `run_script` 白名单扩展到联网/计算类示例（行情、汇率、换算等），
      让这类任务优先走「脚本直算」而非「模型拼 URL」，从根上消除幻觉。
      落点：`src/resolve_harness/tools/builtin.py`（`RUN_SCRIPT_ALLOWLIST`）、`scripts/`、`examples.py`。
- [ ] **关键任务轻量自校验**：对关键产出做非空 / 含真实数据校验（如 `futures.md` 是否含 SHFE 行），
      校验失败触发重跑而非直接交付。落点：`graph/loop.py`（`_compose_deliverable`）。
- [ ] **Evaluator 失败重规划轮数可配置**：现硬编码 1 轮（`graph/loop.py` `re_plan` 分支），
      改为按任务复杂度可配置，复杂任务更稳。

## 二、产出与插件治理

- [ ] **produced files 改扫沙箱目录**：现 UI 仅依赖事件枚举（`produced_file`/`write_file`），
      新增产出通道易漏（前次 `run_script` 子进程写文件即踩过）。改为直接扫描任务沙箱目录列出真实落盘文件。
      落点：`web/src/components/{TaskPanel,HistoryPanel}.tsx`（`producedFilesOf`）、后端任务沙箱路径。
- [ ] **运行时插件质量闸**：任务模式 codegen 成功即写共享 `data/fastpath_plugins/`，无质量过滤，可能污染全局。
      建议持久化前记录来源/命中率，或默认进「候选区」待人工晋升。
      落点：`codegen.py`（`save_plugin`）、`fastpath.py`（`load_plugins`）。

## 三、Fast-path 零模型覆盖

- [ ] **扩充内置匹配器**：闰年、中文数字转阿拉伯、常见单位换算等高频确定性查询压进零模型层，
      减少 codegen 的 LLM 开销。落点：`fastpath.py`。
- [ ] **业务型确定性逻辑探索**：行情/汇率等是否可前置到零模型层（结合上面的「确定性脚本」）。

## 四、工程化与体验

- [ ] **成本 / dry-run 预览**：执行前估算 token / 调用次数，给出预览。
- [ ] **任务中断粒度细化**：现 stop 在任务级，探索子任务级中断。
- [ ] **示例任务可折叠持久化**：示例区折叠状态若需跨刷新保留，可记入 localStorage。

## 五、评审遗留（次优先，本轮未做）

> 来自代码评审（后端/codegen/前端/测试四路并行扫描）的中优先级项，本轮仅修 HIGH，
> 以下留待后续周期。

- [ ] **晋升后进程内不 reload**：`promote_plugins` 删运行时文件但 `fastpath` 绑定在导入期，
      到重启前该模式既不在运行时也不在晋升档（真空档）。晋升后 `importlib.reload(generated_detectors)`。
      落点：`codegen.py` / `fastpath.py:22`。
- [x] **任务内存只增不减**：删 `record.queue`（写了从不读），`_records` 加 `MAX_LIVE_RECORDS=200`
       边界淘汰已结束记录（运行中永不淘汰），`get()` 回退 SQLite 历史。`tasks.py`
- [x] **`announced_batches` 跨 turn 共享且不清**：改为每次 `agent_node` 进入时 `clear()`，
       按 step 去重且集合有界。`graph/loop.py`
- [x] **`usage_snapshot/diff` 读 `total_usage` 未加锁**：读时也取 `router._usage_lock`，
       消除并行任务下「dict changed size during iteration」。`llm.py`
- [x] **`fetch` 无下载上限 + SSRF**：改 `httpx.stream` + `_MAX_DOWNLOAD_BYTES=5MB` 流式截断；
       `_is_safe_host` 拒绝私有/回环/链路本地/保留地址。`tools/http.py`
- [x] **任务模式审批门**：维持「委托式自主执行」（沙箱隔离、无在线审批者），人工门只在交互式
       chat 模式启用（设计决定，非 bug）。如未来要任务内审批，需先加审批者机制。
- [x] **测试/配置治理**：三处 SQLite 仓储（`event_log`/`chat_history`/`long_term`）改**懒连接**，
       import-time `create_app()` 不再触碰磁盘真实库；`test_tasks.py` 共享 `/tmp` 改为 `tmp_path`
       隔离（autouse `_task_env`）。`event_log.py` / `chat_history.py` / `memory/long_term.py` / `tests/test_tasks.py`

## 近期已完成（本周期）

- [x] **安全：codegen 沙箱补 `format_map` 拦截**，堵住「字符串字面量藏 dunder 遍历」旁路。
      `codegen.py:85`（回归测试 `test_format_map_dunder_bypass_rejected`）。
- [x] **安全：`run_script` 强制沙箱内落盘**，丢弃模型传入的 `--out` 绝对/越界路径；
      `_run_script_out_path` 不再信任模型参数。`builtin.py` / `graph/loop.py`
      （回归测试 `test_out_argument_confined_to_sandbox`）。
- [x] **`promote_plugins` 合并保留历史晋升**，不再整体覆盖丢失已晋升检测器。`codegen.py`
      （回归测试 `test_promote_preserves_existing_detectors`）。
- [x] **审批决策解析 bug**：HTTP 层 pydantic 模型在边界 `model_dump()` 转 dict，修复「全部自动拒绝」。
      `api.py:chat_approve`。
- [x] **缺 key 友好报错**：`LLMError` → 400 + 配置指引，替代 502 原始 traceback。`api.py`（chat/approve 两处）。
- [x] **密钥不出配置**：`data/config.json` 明文密钥移入 gitignored 的 `.env`（`SENSENOVA_API_KEY`），
      config 只引用变量名。
- [x] **`produced_file` 离线回归测试**：不依赖网络验证子进程落盘广播。`test_approval.py`

- [x] 修复「停止」任务不生效（`tasks.py` 中断信号 / `api.py` / 前端 stopTask）。
- [x] LLM 收尾 trailing-tool 容错 shim（`llm.py` `_ensure_user_trailing`）。
- [x] 「查期货」示例改用真实 SHFE 数据 + 日期注入。
- [x] 受控 `run_script` 工具（白名单 + 沙箱 + 超时 + 自动落盘）。
- [x] 任务「产出文件」UI 覆盖 `run_script` 子进程落盘（`produced_file` 事件）。
- [x] 示例任务区可折叠（`TaskPanel.tsx` + `index.css`）。
- [x] 「闰年插件」示例任务（走 codegen 流水线，演示插件运行时）。
- [x] 双语 README（英文 `README.md` / 中文 `README.zh.md`，含 Fast-path 亮点）。
- [x] 长期记忆栏左拉可扩展宽度（持久化到 localStorage）。
