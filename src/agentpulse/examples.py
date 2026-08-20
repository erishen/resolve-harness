"""Demo task objectives for the task workbench.

The built-in set covers every capability of the PSE pipeline (math, files,
code) and is shown as the example grid in the task workbench. The list is
fixed and reviewable in source — no dynamic or memory-derived generation.
"""

from __future__ import annotations

BUILTIN_EXAMPLES: list[dict[str, str]] = [
    {"label": "计算", "text": "计算 12 × 34 是多少", "source": "builtin"},
    {"label": "多步计算", "text": "计算 (23+45) 和 (67+89)，并告诉我哪个结果更大", "source": "builtin"},
    {"label": "写文档", "text": "写一份 150 字左右的 RAG 技术简介，保存为沙箱文件 rag-intro.md", "source": "builtin"},
    {"label": "小项目", "text": "创建一个小型 Python 项目：README.md 写项目说明，hello.py 写一个打印问候的脚本", "source": "builtin"},
    {"label": "代码+文档", "text": "写一个 Python 快速排序函数保存为 quicksort.py，并写 100 字使用说明保存为 quicksort-notes.md", "source": "builtin"},
]


def generate_examples() -> list[dict[str, str]]:
    """Return the fixed built-in example objectives.

    Kept as a function (not a bare constant) so callers don't mutate the
    shared list, and the name stays stable for the API wiring.
    """
    return list(BUILTIN_EXAMPLES)
