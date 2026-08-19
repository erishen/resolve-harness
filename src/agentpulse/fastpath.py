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


# -- public entry ----------------------------------------------------------------


def try_fast_answer(text: str) -> FastAnswer | None:
    """Return a FastAnswer when the input is fully resolvable by code, else None.

    Order matters: arithmetic first (higher value), then time.
    """
    if not text or not text.strip():
        return None
    arith = _try_arithmetic(text)
    if arith is not None:
        return arith
    return _try_time(text)
