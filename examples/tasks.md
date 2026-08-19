# agentpulse 多 Agent 编排演示示例

以下是可直接复制到「任务」tab 的目标文本，覆盖 PSE（Planner → Specialist × N → Evaluator）流程的各种演示场景。目标全部基于内置工具（`add` / `write_file` / `read_file` / `remember` / `recall` / `get_current_time`），不需要外部搜索服务，真实模型即可跑通。

> 提示：任务产生的文件会落在沙箱目录 `data/sandbox/` 下；用 `list_files` 工具或在终端 `ls data/sandbox` 可确认产物。

---

## 1. 最简流程 —— 单子任务 + 工具调用

```
计算 12 × 34 是多少
```

**演示看点**：Planner 只拆 1 个子任务 → Specialist 调 `add` → Evaluator 验收通过 → Reporter 交付。几分钟跑完全链路，适合开场快速演示。

---

## 2. 多子任务拆解 —— 子任务间上下文传递

```
计算 (23+45) 和 (67+89)，并告诉我哪个结果更大
```

**演示看点**：Planner 拆 2 个计算子任务 + 1 个比较子任务；Specialist ② 能看到 Specialist ① 的结果（子任务上下文传递），任务树里能看到完整的依赖链。

---

## 3. 文件交付 —— write_file 产出真实文件

```
写一份 150 字左右的 RAG 技术简介，保存为沙箱文件 rag-intro.md
```

**演示看点**：Specialist 调 `write_file` 产出 markdown 文件，`tool_result` 会显示写入路径；执行后 `ls data/sandbox` 能看到 `rag-intro.md`。适合演示"agent 不只是说话，而是真的产出文件"。

---

## 4. 小型项目 —— 多文件产出 + 完整任务树（推荐全场演示）

```
创建一个小型 Python 项目：README.md 写项目说明，hello.py 写一个打印问候的脚本
```

**演示看点**：Planner 拆 2+ 子任务，Specialist 逐个写文件，任务树完整展示 计划 → 执行 → 验收 → 交付。两个交付物文件都能在沙箱里打开验证，说服力最强。

---

## 5. 长期记忆 —— remember / recall 跨子任务

```
记住我的偏好：我喜欢用暗色主题；然后告诉我你记住了什么
```

**演示看点**：Specialist 调 `remember` 写入长期记忆（SQLite），再 `recall` 读回确认；页面右侧「Long-term Memory」面板会实时出现这条记录，刷新浏览器后仍然存在。

---

## 6. 代码 + 文档 —— 综合交付（最完整演示）

```
写一个 Python 快速排序函数保存为 quicksort.py，并写 100 字使用说明保存为 quicksort-notes.md
```

**演示看点**：Planner 拆「写代码」+「写文档」两个子任务，`write_file` 两次落盘；交付区用 markdown 呈现说明。一个目标同时展示 代码产出、文档产出、多子任务 三条能力线。

---

## 进阶：触发 Evaluator 打回重规划

```
帮我写点东西（随便什么都行）
```

**演示看点**：目标过于模糊时，Evaluator 会给出低分并打回（`feedback` 指出缺少具体内容），Planner 带着反馈重规划——界面上能看到「🔄 打回重规划」卡片。适合演示"不是 agent 说完成就完成，有独立验收"。

> 注：是否触发打回取决于模型判断，模糊目标更容易触发；这是特性演示而非故障。
