"""Demo task objectives for the task workbench, optionally personalized from
the user's long-term memory.

The built-in set covers every capability of the PSE pipeline (math, files,
memory, code). When the user has persisted preferences, the model generates a
few extra objectives that fit those preferences — so the demo adapts to the
person, e.g. someone who saved "我喜欢暗色主题" gets a dark-theme task.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .harness import Harness

BUILTIN_EXAMPLES: list[dict[str, str]] = [
    {"label": "计算", "text": "计算 12 × 34 是多少", "source": "builtin"},
    {"label": "多步计算", "text": "计算 (23+45) 和 (67+89)，并告诉我哪个结果更大", "source": "builtin"},
    {"label": "写文档", "text": "写一份 150 字左右的 RAG 技术简介，保存为沙箱文件 rag-intro.md", "source": "builtin"},
    {"label": "小项目", "text": "创建一个小型 Python 项目：README.md 写项目说明，hello.py 写一个打印问候的脚本", "source": "builtin"},
    {"label": "记忆", "text": "记住我的偏好：我喜欢用暗色主题；然后告诉我你记住了什么", "source": "builtin"},
    {"label": "代码+文档", "text": "写一个 Python 快速排序函数保存为 quicksort.py，并写 100 字使用说明保存为 quicksort-notes.md", "source": "builtin"},
]

PERSONALIZED_PROMPT = """你是示例任务生成器。根据用户长期记忆中的偏好，生成 3 个「个性化示例任务」，用于演示多 Agent 编排（Planner 拆解 → Specialist 执行 → Evaluator 验收）。

用户长期记忆偏好：
{memories}

沙箱当前已有的文件（任务可以读取/分析它们，也可以自行创建新数据）：
{sandbox_files}

生成要求：
1. 自足可执行：任务不需要沙箱外任何东西；引用文件时优先用上面列出的已有文件；若要新文件，任务必须自己先创建（如「生成 10 个样本数据保存为 data.csv，再统计」）。禁止引用任何不存在的路径，尤其禁止 /app、/tmp、/root 等根目录绝对路径。
2. 偏好体现在任务的内容与主题上（如偏好暗色主题 → 「写一个暗色主题的 HTML 页面保存为 dark.html」「计算一组颜色在深色背景上的对比度并输出报告」），而不是机械地给无关操作加前缀。
3. 每个任务要有可验证的产出：计算结果 / 保存的文件 / 报告，便于 Evaluator 验收。
4. label 用 2-6 个字的简短中文标签（如「暗色页面」「配色报告」）；text 是具体可执行的一句话任务描述（20-80 字）。
5. 避免：开放式写作题、无法验证结果的任务、引用不存在的文件、空泛描述。

输出 ONLY JSON 数组：[{{"label": "简短中文标签", "text": "完整任务目标"}}]
不要解释、不要多余文字。

好例子：
[{{"label": "暗色页面", "text": "创建暗色主题的 HTML 页面 dark.html，包含标题、一段说明文字和一个表格，保存到沙箱"}}]

坏例子（引用不存在的文件，绝对禁止）：
[{{"label": "改配置", "text": "将 /app/config/editor.json 中所有 theme 字段改为 dark"}}]"""

# root absolute paths can never resolve inside the sandbox -> reject
_ROOT_PATH_RE = re.compile(r"/(?:app|tmp|root|home|var|etc|usr|opt|srv|dev)(?:/|$)")
# labels that are pure latin identifiers look alien next to the Chinese built-ins
_ENGLISH_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{3,}$")

# module-level cache: personalization is regenerated on demand (force=True)
_cache: list[dict[str, str]] | None = None


def _format_memories(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(无)"
    return "\n".join(f"- {r['key']} = {r['value']}" for r in rows)


def _sandbox_file_list(sandbox_dir: str | Path | None) -> str:
    """Relative listing of sandbox files, so the model can reference real paths."""
    base = Path(sandbox_dir) if sandbox_dir else None
    if base is None or not base.is_dir():
        return "(沙箱为空)"
    lines = [f"- {p.relative_to(base).as_posix()}" for p in sorted(base.rglob("*")) if p.is_file()]
    return "\n".join(lines[:20]) or "(沙箱为空)"


def _clean_personalized(items: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Keep only entries that look executable & on-style; drop the rest."""
    if not items:
        return []
    clean: list[dict[str, str]] = []
    for e in items:
        if not isinstance(e, dict):
            continue
        label = str(e.get("label", "")).strip()
        text = str(e.get("text", "")).strip()
        if not label or not text or len(text) < 10:
            continue
        if _ROOT_PATH_RE.search(text):
            continue  # references a root path that can't resolve in the sandbox
        if _ENGLISH_LABEL_RE.match(label):
            continue  # keep labels short Chinese words, matching the built-ins
        clean.append({"label": label[:12], "text": text[:500], "source": "personalized"})
    return clean[:4]


def _extract_json_array(text: str) -> list[dict[str, Any]] | None:
    """Best-effort parse of a JSON array (possibly inside markdown fences)."""
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def generate_examples(harness: Harness, *, force: bool = False) -> list[dict[str, str]]:
    """Return built-in examples plus, when the user has memories, personalized ones.

    Personalized objectives are generated from the user's memories + the actual
    sandbox file listing (so they stay executable), validated against obvious
    landmines (root absolute paths, empty prose), and retried once before
    falling back to built-ins only.

    Cached in-process; `force=True` regenerates the personalized part.
    """
    global _cache
    if not force and _cache is not None:
        return _cache

    personalized: list[dict[str, str]] = []
    rows = harness.long_term.search(scope=harness.memory_scope)
    if rows:
        prompt = PERSONALIZED_PROMPT.format(
            memories=_format_memories(rows),
            sandbox_files=_sandbox_file_list(harness.sandbox_dir),
        )
        for _ in range(2):  # one retry when the model output is unusable
            try:
                response = harness.router.complete(
                    [{"role": "user", "content": prompt}],
                    temperature=0.8,
                )
                parsed = _extract_json_array(response.get("content") or "")
                personalized = _clean_personalized(parsed)
            except Exception:  # noqa: BLE001 - personalization is best-effort
                personalized = []
            if personalized:
                break

    result = BUILTIN_EXAMPLES + personalized
    _cache = result
    return result
