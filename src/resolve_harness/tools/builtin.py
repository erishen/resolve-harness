"""Built-in tools bundled with the harness.

These demonstrate the tool contract and are useful on their own:

- get_current_time  — wall clock (no network needed)
- remember / recall — read & write long-term memory (the Memory pillar)

Arithmetic has no tool: pure math queries are resolved by the Fast Path
(code) before any model call, so an `add` tool would rarely fire.

Extra tools can be registered on the harness with `h.tools.register(...)`.
"""

from __future__ import annotations

import datetime
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from ..memory import LongTermMemory
from .registry import ToolRegistry


# 受控脚本白名单：run_script 只能运行 scripts/ 下这里列出的已知安全脚本，
# 杜绝「任意命令执行」。新增可运行脚本必须显式加入此集合。
RUN_SCRIPT_ALLOWLIST = frozenset({"fetch_shfe_futures"})


def _make_run_script_tool(
    sandbox_dir: str | None,
    scripts_dir: str | Path | None = None,
) -> Callable[..., str]:
    """返回一个 run_script 工具：在沙箱内运行 scripts/ 下白名单脚本，返回其输出。

    网络与文件写入都被限制在沙箱目录内（cwd=sandbox_dir），属于受控能力——
    用于让 Python 确定性地完成需要联网/计算的任务（如抓取上期所行情并排序），
    避免模型自己拼 URL 或猜日期导致失败。
    """
    base = Path(scripts_dir) if scripts_dir else Path(__file__).resolve().parents[3] / "scripts"

    def run_script(name: str, args: str = "") -> str:
        """在沙箱内运行项目 scripts/ 下受信任的脚本（白名单），返回标准输出/错误。

        仅允许白名单内的脚本名（如 fetch_shfe_futures）。脚本在沙箱目录内运行，
        网络与文件均受限于该目录；适合让 Python 确定性完成联网/计算类任务。"""
        if sandbox_dir is None:
            return "run_script 不可用：未配置沙箱目录"
        if name not in RUN_SCRIPT_ALLOWLIST:
            return f"不允许的脚本：{name}（仅允许 {sorted(RUN_SCRIPT_ALLOWLIST)}）"
        script = base / f"{name}.py"
        if not script.exists():
            return f"脚本不存在：{script}"
        cmd = [sys.executable, str(script)]
        # 产出文件始终强制写进沙箱内的固定相对名（futures.md），并丢弃模型可能
        # 传入的 --out，杜绝其用绝对/越界路径（如 --out /etc/passwd）逃出沙箱目录。
        if args.strip():
            toks = shlex.split(args)
            cleaned: list[str] = []
            skip = False
            for t in toks:
                if skip:
                    skip = False
                    continue
                if t == "--out":
                    skip = True
                    continue
                if t.startswith("--out="):
                    continue
                cleaned.append(t)
            cmd += cleaned
        cmd += ["--out", "futures.md"]
        try:
            proc = subprocess.run(
                cmd, cwd=sandbox_dir, capture_output=True, text=True, timeout=90
            )
        except subprocess.TimeoutExpired:
            return "脚本执行超时（>90s），已终止"
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > 8000:
            out = out[:8000] + "\n…(输出过长已截断)"
        return f"[run_script {name}] 退出码 {proc.returncode}\n{out}"

    run_script.__name__ = "run_script"
    return run_script


def get_current_time() -> str:
    """返回当前本地时间（ISO 8601）。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _make_memory_tools(memory: LongTermMemory) -> list:
    def remember(key: str, value: Any, scope: str = "default") -> str:
        """把事实存入长期记忆（用户偏好、关键结果）。"""
        memory.remember(key, value, scope=scope)
        return f"已存储 '{key}'（scope={scope}）"

    def recall(key: str, scope: str = "default") -> str:
        """从长期记忆读取事实（不存在时返回 null）。"""
        value = memory.recall(key, scope=scope, default=None)
        return f"{key} = {value}" if value is not None else f"在 scope '{scope}' 中未找到 '{key}'"

    def list_memories(scope: str = "default") -> str:
        """列出指定 scope 下的全部记忆 key。"""
        rows = memory.search(scope=scope)
        if not rows:
            return f"scope '{scope}' 中没有记忆"
        return "\n".join(f"- {r['key']}" for r in rows)

    return [remember, recall, list_memories]


def register_builtins(
    registry: ToolRegistry,
    memory: LongTermMemory,
    *,
    sandbox_dir: str | None = None,
) -> None:
    """Register the built-in toolset into a registry, wiring memory tools.

    Args:
        registry: target registry.
        memory: long-term memory the remember/recall tools bind to.
        sandbox_dir: optional sandbox root for filesystem tools
            (read_file / write_file / list_files). When None, fs tools are
            not registered.
    """
    registry.register(get_current_time)
    for fn in _make_memory_tools(memory):
        registry.register(fn)
    if sandbox_dir is not None:
        from .fs import make_fs_tools

        for fn in make_fs_tools(sandbox_dir):
            # write_file mutates the sandbox - route it through the
            # human-approval gate; read_file / list_files are side-effect free.
            registry.register(fn, require_approval=fn.__name__ == "write_file")
    from .http import fetch

    # fetch is the only network egress: a human should okay each request.
    registry.register(fetch, require_approval=True)
    # 受控脚本运行：白名单内脚本在沙箱内执行（用于确定性联网/计算任务）。
    if sandbox_dir is not None:
        registry.register(_make_run_script_tool(sandbox_dir), require_approval=True)
