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
from typing import Any

from .harness import Harness

BUILTIN_EXAMPLES: list[dict[str, str]] = [
    {"label": "计算", "text": "计算 12 × 34 是多少"},
    {"label": "多步计算", "text": "计算 (23+45) 和 (67+89)，并告诉我哪个结果更大"},
    {"label": "写文档", "text": "写一份 150 字左右的 RAG 技术简介，保存为沙箱文件 rag-intro.md"},
    {"label": "小项目", "text": "创建一个小型 Python 项目：README.md 写项目说明，hello.py 写一个打印问候的脚本"},
    {"label": "记忆", "text": "记住我的偏好：我喜欢用暗色主题；然后告诉我你记住了什么"},
    {"label": "代码+文档", "text": "写一个 Python 快速排序函数保存为 quicksort.py，并写 100 字使用说明保存为 quicksort-notes.md"},
]

PERSONALIZED_PROMPT = """你是示例任务生成器。根据用户的长期记忆偏好，生成 3 个适合该用户的示例任务目标，用于演示多 Agent 编排（规划→执行→验收）。

用户长期记忆：
{memories}

要求：
- 每个任务都要贴合用户偏好，同时能展示 PSE 流程（拆解子任务 / 工具调用 / 文件产出）
- 必须是具体可执行的任务（算术、写文件、整理、转换等），不要开放写作题
- 输出 ONLY JSON 数组：[{{"label": "简短标签", "text": "完整任务目标"}}]
- 不要解释、不要多余文字"""

# module-level cache: personalization is regenerated on demand (force=True)
_cache: list[dict[str, str]] | None = None


def _format_memories(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(无)"
    return "\n".join(f"- {r['key']} = {r['value']}" for r in rows)


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

    Cached in-process; `force=True` regenerates the personalized part.
    """
    global _cache
    if not force and _cache is not None:
        return _cache

    personalized: list[dict[str, str]] = []
    rows = harness.long_term.search(scope=harness.memory_scope)
    if rows:
        try:
            response = harness.router.complete(
                [
                    {
                        "role": "user",
                        "content": PERSONALIZED_PROMPT.format(memories=_format_memories(rows)),
                    }
                ],
                temperature=0.8,
            )
            parsed = _extract_json_array(response.get("content") or "")
            if parsed is not None:
                personalized = [
                    {"label": str(e.get("label", ""))[:20], "text": str(e.get("text", ""))[:500]}
                    for e in parsed
                    if isinstance(e, dict) and e.get("label") and e.get("text")
                ][:4]
        except Exception:  # noqa: BLE001 - personalization is best-effort
            personalized = []

    result = BUILTIN_EXAMPLES + personalized
    _cache = result
    return result
