"""Deterministic fast path: answer certain queries with pure code, no LLM.

Many "tasks" are actually deterministic — arithmetic ("计算 2+3"), time
("现在几点"), comparisons of computed values. Running the LLM on these is
slow, costly and error-prone. `try_fast_answer` pattern-matches the input
and, when it can fully resolve it, returns a ready-made answer.

Safety: expressions are evaluated with an AST whitelist (numbers, binary
operators, unary +/-) — arbitrary code is never executed.
"""

from __future__ import annotations

import ast
import datetime
import operator
import re
from dataclasses import dataclass
from typing import Any

# -- safe arithmetic -----------------------------------------------------------

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    raise ValueError("unsupported expression")


def safe_eval_math(expr: str) -> float:
    """Evaluate a pure math expression safely; raises ValueError if unsafe."""
    expr = expr.replace("×", "*").replace("x", "*").replace("X", "*").replace("÷", "/")
    expr = expr.replace("^", "**")
    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree)


# -- pattern matching ------------------------------------------------------------

# 匹配一个完整算术项（含括号或二元运算），如 (23+45)、2+3、7*8
_EXPR = r"(?:\([^()]*\d[\d+\-*/×x÷\s.()]*\)|-?\d+(?:\.\d+)?(?:\s*[+\-*/×x÷^]\s*[+-]?\d+(?:\.\d+)?)+)"
_EXPR_RE = re.compile(_EXPR)

# 中文运算符词（用于"23 加 45"这类输入）
_CN_OPS = [
    ("乘以", "*"), ("乘", "*"), ("除以", "/"), ("除", "/"),
    ("加上", "+"), ("加", "+"), ("减去", "-"), ("减", "-"),
]

_TIME_RE = re.compile(r"现在几点|几点了|当前时间|现在时间|什么时间|what\s*(?:is\s*)?time|current\s*time", re.IGNORECASE)

_COMPARE_RE = re.compile(r"哪个|谁更|比较大|更(?:大|小)|比一比|compare", re.IGNORECASE)


@dataclass
class FastAnswer:
    query: str          # 原始输入
    method: str         # "arithmetic" | "time"
    answer: str         # 给用户的最终回复
    detail: str         # 计算过程（供 tool_result 展示）


def _normalize_cn(text: str) -> str:
    """把中文运算符词转成符号（仅处理独立出现的算术词，避免误伤）。"""
    for cn, sym in _CN_OPS:
        text = text.replace(cn, sym)
    return text


def _match_arithmetic(text: str) -> list[str]:
    """提取文本中所有可安全求值的算术表达式（去重保序）。"""
    norm = _normalize_cn(text)
    found: list[str] = []
    for m in _EXPR_RE.finditer(norm):
        expr = m.group(0).strip()
        # 单个数字不是表达式
        if re.fullmatch(r"-?\d+(?:\.\d+)?", expr):
            continue
        try:
            safe_eval_math(expr)
        except Exception:  # noqa: BLE001 - unsafe/unparseable, skip
            continue
        if expr not in found:
            found.append(expr)
    return found


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _try_arithmetic(text: str) -> FastAnswer | None:
    exprs = _match_arithmetic(text)
    if not exprs:
        return None
    results = [(expr, _eval_and_format(expr)) for expr in exprs]
    lines = [f"{expr} = {val}" for expr, val in results]
    answer_lines = list(lines)

    # 比较意图：若输入要求比较，把结果比一比
    if len(results) >= 2 and _COMPARE_RE.search(text):
        vals = [float(safe_eval_math(expr)) for expr, _ in results]
        max_val = max(vals)
        max_idx = vals.index(max_val)
        if len(set(vals)) == 1:
            answer_lines.append("两个结果相等")
        else:
            answer_lines.append(
                f"其中 {results[max_idx][0]} = {results[max_idx][1]} 更大"
            )

    answer = "，".join(answer_lines) + "。"
    return FastAnswer(
        query=text,
        method="arithmetic",
        answer=answer,
        detail="\n".join(lines),
    )


def _eval_and_format(expr: str) -> str:
    return _format_number(safe_eval_math(expr))


_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _try_time(text: str) -> FastAnswer | None:
    if not _TIME_RE.search(text):
        return None
    now = datetime.datetime.now().astimezone()
    weekday = _WEEKDAYS[now.weekday()]
    answer = f"现在是 {now.strftime('%Y-%m-%d %H:%M:%S')}（{weekday}）。"
    return FastAnswer(
        query=text,
        method="time",
        answer=answer,
        detail=now.isoformat(timespec="seconds"),
    )


# -- statistics / sorting --------------------------------------------------------

_NUM_LIST_RE = re.compile(r"-?\d+(?:\.\d+)?")
_STAT_INTENT = {
    "最大": ("max", "最大值为"),
    "最大数": ("max", "最大值为"),
    "最小": ("min", "最小值为"),
    "平均": ("avg", "平均值为"),
    "总和": ("sum", "总和为"),
    "求和": ("sum", "总和为"),
    "排序": ("sort", "从小到大为"),
    "从小到大": ("sort", "从小到大为"),
    "从大到小": ("sort_desc", "从大到小为"),
}


def _try_statistics(text: str) -> FastAnswer | None:
    intent = None
    for kw, (kind, label) in _STAT_INTENT.items():
        if kw in text:
            intent = (kind, label)
            break
    if intent is None:
        return None
    nums = [float(m) for m in _NUM_LIST_RE.findall(text)]
    if len(nums) < 2:
        return None
    kind, label = intent
    if kind == "max":
        value = max(nums)
    elif kind == "min":
        value = min(nums)
    elif kind == "avg":
        value = sum(nums) / len(nums)
    elif kind == "sum":
        value = sum(nums)
    elif kind == "sort":
        value = sorted(nums)
        return FastAnswer(text, "statistics", f"{label} {'、'.join(_format_number(v) for v in value)}。", "、".join(_format_number(v) for v in value))
    else:  # sort_desc
        value = sorted(nums, reverse=True)
        return FastAnswer(text, "statistics", f"{label} {'、'.join(_format_number(v) for v in value)}。", "、".join(_format_number(v) for v in value))
    return FastAnswer(text, "statistics", f"{label} {_format_number(value)}。", str(value))


# -- unit conversion ----------------------------------------------------------------

_CONVERSIONS: list[tuple[re.Pattern, str, str, float, str]] = [
    # (pattern, unit_name, target_unit, factor, kind)
    # 目标单位前允许出现 多少/几 等疑问词
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:摄氏)度\s*(?:等于|换算成|是|转|到|多少|几)*\s*(?:华氏|fahrenheit)", re.I), "摄氏→华氏", "华氏度", None, "c2f"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:华氏)度\s*(?:等于|换算成|是|转|到|多少|几)*\s*(?:摄氏|celsius)", re.I), "华氏→摄氏", "摄氏度", None, "f2c"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:km|公里)\s*(?:等于|换算成|是|转|到|多少|几)*\s*(?:mi|英里)", re.I), "公里→英里", "英里", 0.621371, None),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:mi|英里)\s*(?:等于|换算成|是|转|到|多少|几)*\s*(?:km|公里)", re.I), "英里→公里", "公里", 1.609344, None),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:kg|千克|公斤)\s*(?:等于|换算成|是|转|到|多少|几)*\s*(?:lb|磅)", re.I), "千克→磅", "磅", 2.204623, None),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:斤)\s*(?:等于|换算成|是|转|到|多少|几)*\s*(?:kg|千克|公斤)", re.I), "斤→千克", "千克", 0.5, None),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:小时|h)\s*(?:等于|换算成|是|转|到|多少|几)*\s*(?:分钟|min)", re.I), "小时→分钟", "分钟", 60.0, None),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:分钟|min)\s*(?:等于|换算成|是|转|到|多少|几)*\s*(?:小时|h)", re.I), "分钟→小时", "小时", 1 / 60.0, None),
]


def _try_unit_convert(text: str) -> FastAnswer | None:
    for pat, label, target, factor, kind in _CONVERSIONS:
        m = pat.search(text)
        if not m:
            continue
        value = float(m.group(1))
        if kind == "c2f":
            result = value * 9 / 5 + 32
        elif kind == "f2c":
            result = (value - 32) * 5 / 9
        else:
            result = value * factor
        display = _format_number(round(result, 2))
        return FastAnswer(
            text,
            "unit_convert",
            f"{_format_number(value)} 换算成 {target} 为 {display}。",
            f"{value} {label} = {result}",
        )
    return None


# -- date math -------------------------------------------------------------------------

_DATE_MATH_RE = re.compile(r"(明天|昨天|后天|前天)")
_DAYS_LATER_RE = re.compile(r"(\d+)\s*天(?:之|以)?(?:后|前)")
_DATE_DIFF_RE = re.compile(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\s*(?:和|与|到|和|跟)\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\s*(?:相差|差|相隔)?\s*几天")


def _parse_date(s: str) -> datetime.date:
    for sep in ("-", "/", "."):
        if sep in s:
            y, m, d = s.split(sep)
            return datetime.date(int(y), int(m), int(d))
    raise ValueError(f"bad date: {s}")


def _try_date_math(text: str) -> FastAnswer | None:
    today = datetime.date.today()
    m = _DATE_DIFF_RE.search(text)
    if m:
        d1, d2 = _parse_date(m.group(1)), _parse_date(m.group(2))
        delta = abs((d2 - d1).days)
        return FastAnswer(text, "date_math", f"{d1} 和 {d2} 相差 {delta} 天。", str(delta))
    m = _DAYS_LATER_RE.search(text)
    if m:
        days = int(m.group(1))
        direction = 1 if "后" in m.group(0) else -1
        target = today + datetime.timedelta(days=days * direction)
        return FastAnswer(text, "date_math", f"{days} 天{direction > 0 and '后' or '前'}是 {target.isoformat()}（{_WEEKDAYS[target.weekday()]}）。", target.isoformat())
    m = _DATE_MATH_RE.search(text)
    if m:
        kw = m.group(1)
        offset = {"明天": 1, "后天": 2, "昨天": -1, "前天": -2}[kw]
        target = today + datetime.timedelta(days=offset)
        return FastAnswer(text, "date_math", f"{kw}是 {target.isoformat()}（{_WEEKDAYS[target.weekday()]}）。", target.isoformat())
    return None


# -- base conversion ---------------------------------------------------------------------

_BASE_RE = re.compile(r"(\d+)\s*的\s*(二进制|八进制|十六进制|二进制数|八进制数|十六进制数)|(二进制|八进制|十六进制)\s*(?:的|表示|形式)?\s*(\d+)", re.I)
_BASE_MAP = {"二进制": 2, "八进制": 8, "十六进制": 16}


def _try_base_convert(text: str) -> FastAnswer | None:
    m = _BASE_RE.search(text)
    if not m:
        return None
    num_str, base_name = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    if not num_str or not base_name:
        return None
    base_name = base_name.replace("数", "")
    base = _BASE_MAP.get(base_name)
    if base is None:
        return None
    value = int(num_str)
    if base == 2:
        result = bin(value)[2:]
    elif base == 8:
        result = oct(value)[2:]
    else:
        result = hex(value)[2:]
    return FastAnswer(text, "base_convert", f"{num_str} 的{base_name}是 {result}。", result)


# -- text statistics -------------------------------------------------------------------------

_CHAR_COUNT_RE = re.compile(r"(?:有|一共|统计|数一数)?\s*几个(?:字|字符)|多少(?:个)?(?:字|字符)")


def _try_text_stats(text: str) -> FastAnswer | None:
    if not _CHAR_COUNT_RE.search(text):
        return None
    # 引号/书名号内的文本优先作为统计对象
    quoted = re.findall(r"[\"“「]([^\"”」]+)[\"”」]", text)
    body = quoted[0] if quoted else _CHAR_COUNT_RE.sub("", text).strip()
    body = body.strip('"“”「」')
    if not body:
        return None
    chars = len(body)
    return FastAnswer(text, "text_stats", f"「{body}」共 {chars} 个字（含标点）。", str(chars))


# -- sandbox files -----------------------------------------------------------------------------

_SANDBOX_LIST_RE = re.compile(r"(?:沙箱|sandbox).*(?:有哪些|列出|列表|list)|(?:列出|看看|查看).*(?:沙箱|sandbox).*(?:文件|内容)", re.I)
_SANDBOX_READ_RE = re.compile(r"(?:读取|查看|读一下|打开)\s*(?:沙箱|sandbox)(?:里|下|中)?的?\s*([\w./\-]+\.\w+)")


def _sandbox_list(sandbox_dir: str | None) -> FastAnswer | None:
    from pathlib import Path

    if not sandbox_dir:
        return None
    root = Path(sandbox_dir)
    if not root.exists():
        return FastAnswer("sandbox", "sandbox_files", "沙箱目录还不存在。", "")
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    if not files:
        return FastAnswer("sandbox", "sandbox_files", "沙箱是空的，还没有文件。", "")
    return FastAnswer(
        "sandbox",
        "sandbox_files",
        "沙箱中有 " + str(len(files)) + " 个文件：" + "、".join(files) + "。",
        "\n".join(f"- {f}" for f in files),
    )


def _sandbox_read(sandbox_dir: str | None, path: str) -> FastAnswer | None:
    from pathlib import Path

    if not sandbox_dir:
        return None
    root = Path(sandbox_dir)
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return FastAnswer("sandbox", "sandbox_files", f"沙箱中不存在文件 {path}。", "")
    text = target.read_text(encoding="utf-8")
    preview = text[:500] + ("…" if len(text) > 500 else "")
    return FastAnswer(
        "sandbox",
        "sandbox_files",
        f"「{path}」（{len(text)} 字符）内容：\n{preview}",
        f"{path} ({len(text)} chars)",
    )


# -- public entry ----------------------------------------------------------------


def try_fast_answer(text: str, *, sandbox_dir: str | None = None) -> FastAnswer | None:
    """Return a FastAnswer when the input is fully resolvable by code, else None.

    Order matters: cheap/high-value matchers run first.
    """
    if not text or not text.strip():
        return None

    checks: list = [
        _try_arithmetic,
        _try_statistics,
        _try_unit_convert,
        _try_date_math,
        _try_base_convert,
        _try_text_stats,
        _try_time,
    ]
    for check in checks:
        result = check(text)
        if result is not None:
            return result

    # sandbox needs context (path), so it's handled separately
    if _SANDBOX_LIST_RE.search(text):
        result = _sandbox_list(sandbox_dir)
        if result is not None:
            return result
    m = _SANDBOX_READ_RE.search(text)
    if m:
        result = _sandbox_read(sandbox_dir, m.group(1))
        if result is not None:
            return result
    return None
