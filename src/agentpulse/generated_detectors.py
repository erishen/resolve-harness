"""晋升的 fast-path 检测器（插件整理生成；函数体可自由修改，整体结构勿手改）。

由管理界面从 data/fastpath_plugins/ 选中合并生成。每个函数对应一个曾在
运行时生成过的确定性模式，晋升后成为内置检测器，随源码一起提交。
"""

from typing import Callable


def detect_promoted_1(text: str) -> str | None:
    """trigger: 把 12345 倒过来写"""
    try:
        import re
        if '倒过来' not in text:
            return None
        match = re.search(r'\d+', text)
        if not match:
            return None
        num_str = match.group()
        return f"把 {num_str} 倒过来写是 {num_str[::-1]}。"
    except Exception:
        return None


def detect_promoted_2(text: str) -> str | None:
    """trigger: 计算 7 的阶乘"""
    try:
        import re
        import math
        match = re.search(r'(\d+)\s*的阶乘', text)
        if not match:
            return None
        n = int(match.group(1))
        result = math.factorial(n)
        return f"{n} 的阶乘等于 {result}。"
    except Exception:
        return None


DETECTORS: list[Callable[[str], str | None]] = [detect_promoted_1, detect_promoted_2]
