"""Demo task objectives for the task workbench.

The built-in set covers every capability of the PSE pipeline (math, files,
code) and is shown as the example grid in the task workbench. Because the
workbench drives a live agent, the user can also ask the model to *regenerate*
a fresh batch of example tasks — generic and varied, **not** derived from any
saved memory — so the demos keep feeling fresh on each click.

Deletion: any example (built-in or generated) can be deleted from the grid.
Deletions are persisted as a tombstone list in `data/deleted_examples.json`,
so deleted examples stay hidden across reloads, and the regeneration prompt
receives the tombstones as a negative list ("don't generate anything similar").
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
    {"label": "代码+文档", "text": "写一个 Python 快速排序函数保存为 quicksort.py，并写 100 字使用说明保存为 quicksort-notes.md", "source": "builtin"},
    {"label": "抓 JD", "text": "用 fetch 工具获取 https://remotive.com/api/remote-jobs?search=python 的 python 岗位 JD 列表，提取前 3 个岗位的标题、公司与工作地点，汇总保存为沙箱文件 python-jobs.md", "source": "builtin"},
    {"label": "查期货", "text": "调用 run_script 工具运行脚本 fetch_shfe_futures：该脚本会自动取今天日期、抓取上海期货交易所官网每日行情、按涨跌幅取前 5 大期货合约，并写入沙箱文件 futures.md（脚本内部已处理日期与非交易日回滚，无需你手动拼 URL 或猜日期）。运行后查看 futures.md，确认内容包含前 5 大涨幅合约的合约名称（PRODUCTNAME+DELIVERYMONTH）、最新价（CLOSEPRICE）与涨跌幅（真实数据，非占位）。", "source": "builtin"},
    {"label": "闰年插件", "text": "判断给定年份是否为闰年并简述规则（如问『2024 年是闰年吗』应回答『是闰年』）。该问题为纯确定性逻辑，请走 fast-path 插件运行时：由 codegen 生成 detect 检测器并持久化到 data/fastpath_plugins，之后同类问题零模型命中 load_plugins 缓存复用。", "source": "builtin"},
]

# Generic regeneration prompt — deliberately NOT memory-aware. It only asks the
# model for fresh, varied, executable demo tasks that exercise the PSE pipeline.
GENERATE_PROMPT = """你是示例任务生成器。为多 Agent 编排系统（Planner 拆解 → Specialist 执行 → Evaluator 验收）生成 4 个「示例任务」，用于演示系统能力。

沙箱当前已有的文件（任务可以读取/分析它们，也可以自行创建新数据）：
{sandbox_files}

生成要求：
1. 自足可执行：任务不需要沙箱外任何东西；引用文件时优先用上面列出的已有文件；若要新文件，任务必须自己先创建（如「生成 10 个样本数据保存为 data.csv，再统计」）。禁止引用任何不存在的路径，尤其禁止 /app、/tmp、/root 等根目录绝对路径。
2. 覆盖多样性：混合数学计算、文件读写、写代码、写文档、小项目等不同题型，避免和内置示例（计算/写文档/小项目/代码+文档）完全重复。
3. 每个任务要有可验证的产出：计算结果 / 保存的文件 / 报告，便于 Evaluator 验收。
4. label 用 2-6 个字的简短中文标签（如「配色报告」「数据清洗」）；text 是具体可执行的一句话任务描述（20-80 字）。
5. 避免：开放式写作题、无法验证结果的任务、引用不存在的文件、空泛描述。

{deleted_block}

输出 ONLY JSON 数组：[{{"label": "简短中文标签", "text": "完整任务目标"}}]
不要解释、不要多余文字。

好例子：
[{{"label": "数据清洗", "text": "生成 12 个带噪声的温度读数保存为 temps.csv，写脚本清洗异常值并输出均值与最大值"}}]

坏例子（引用不存在的文件，绝对禁止）：
[{{"label": "改配置", "text": "将 /app/config/editor.json 中所有 theme 字段改为 dark"}}]"""

# root absolute paths can never resolve inside the sandbox -> reject
_ROOT_PATH_RE = re.compile(r"/(?:app|tmp|root|home|var|etc|usr|opt|srv|dev)(?:/|$)")
# labels that are pure latin identifiers look alien next to the Chinese built-ins
_ENGLISH_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{3,}$")

# module-level cache: regenerated examples are reused until force=True
_cache: list[dict[str, str]] | None = None


# -- deleted-example tombstones ---------------------------------------------------
#
# Deleted examples (built-in or generated) are persisted as a tombstone list so
# they stay hidden across reloads, and regeneration is told to avoid them.


def _deleted_file() -> Path:
    """Tombstone store location: <project root>/data/deleted_examples.json."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent / "data" / "deleted_examples.json"
    return Path.cwd() / "data" / "deleted_examples.json"


def _load_deleted() -> list[dict[str, str]]:
    """Read the tombstone list; corrupt/missing file degrades to empty."""
    path = _deleted_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("deleted") if isinstance(data, dict) else None
        if isinstance(items, list):
            return [{"label": str(e.get("label", "")), "text": str(e.get("text", ""))} for e in items]
    except (OSError, ValueError, AttributeError):
        pass
    return []


def _save_deleted(items: list[dict[str, str]]) -> None:
    path = _deleted_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"deleted": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _deleted_texts() -> set[str]:
    return {e["text"] for e in _load_deleted() if e.get("text")}


def _deleted_block() -> str:
    """Negative list for the regeneration prompt: avoid anything similar."""
    deleted = [e for e in _load_deleted() if e.get("text")]
    if not deleted:
        return ""
    lines = "\n".join(
        f"- {e.get('label') or '未命名'}：{e['text']}" for e in deleted[-15:]
    )
    return (
        "以下任务已被用户删除，**不要生成与它们类似的任务**"
        "（避免重复的标签、主题或写法）：\n" + lines
    )


def delete_example(label: str, text: str) -> bool:
    """Tombstone one example (built-in or generated). Idempotent.

    Returns True when the example was newly recorded, False when it was
    already deleted. Also invalidates the regeneration cache so a deleted
    generated example cannot resurface from the cached batch.
    """
    text = (text or "").strip()
    if not text:
        return False
    items = _load_deleted()
    if any(e.get("text") == text for e in items):
        return False
    items.append({"label": (label or "").strip()[:80], "text": text[:500]})
    _save_deleted(items)
    global _cache
    _cache = None  # a deleted generated example must not come back from cache
    return True


def _filter_deleted(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop any tombstoned example from a result list."""
    deleted = _deleted_texts()
    if not deleted:
        return list(items)
    return [e for e in items if e.get("text") not in deleted]


def _sandbox_file_list(sandbox_dir: str | Path | None) -> str:
    """Relative listing of sandbox files, so the model can reference real paths."""
    base = Path(sandbox_dir) if sandbox_dir else None
    if base is None or not base.is_dir():
        return "(沙箱为空)"
    lines = [f"- {p.relative_to(base).as_posix()}" for p in sorted(base.rglob("*")) if p.is_file()]
    return "\n".join(lines[:20]) or "(沙箱为空)"


def _clean_generated(items: list[dict[str, Any]] | None) -> list[dict[str, str]]:
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
        clean.append({"label": label[:12], "text": text[:500], "source": "generated"})
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


def generate_examples() -> list[dict[str, str]]:
    """Return the fixed built-in example objectives (no LLM, fast path).

    Deleted examples (tombstoned via `delete_example`) are excluded.
    """
    return _filter_deleted(BUILTIN_EXAMPLES)


def generate_fresh_examples(harness: Harness, *, force: bool = False) -> list[dict[str, str]]:
    """Return built-ins plus a fresh batch of generic LLM-generated demo tasks.

    The generated tasks are varied and executable (validated against root
    absolute paths, off-style labels, retried once). They are NOT derived
    from the user's long-term memory — just fresh demos on each regeneration.

    Cached in-process; `force=True` regenerates the generated part. Deleted
    examples are filtered from the result and fed to the model as a negative
    list so fresh batches avoid anything similar.
    """
    global _cache
    if not force and _cache is not None:
        return _filter_deleted(_cache)

    generated: list[dict[str, str]] = []
    prompt = GENERATE_PROMPT.format(
        sandbox_files=_sandbox_file_list(harness.sandbox_dir),
        deleted_block=_deleted_block(),
    )
    for _ in range(2):  # one retry when the model output is unusable
        try:
            response = harness.router.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.9,
            )
            parsed = _extract_json_array(response.get("content") or "")
            generated = _clean_generated(parsed)
        except Exception:  # noqa: BLE001 - generation is best-effort
            generated = []
        if generated:
            break

    result = _filter_deleted(BUILTIN_EXAMPLES + generated)
    _cache = result
    return result
