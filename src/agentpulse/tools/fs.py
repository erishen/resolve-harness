"""Filesystem tools, confined to a sandbox directory.

These let the agent actually *do work* (write a report, keep notes, build a
small artifact) instead of only answering. Safety: every path is resolved
against `sandbox_dir` and any traversal outside it is rejected — the agent
cannot touch anything else on disk.
"""

from __future__ import annotations

from pathlib import Path


class SandboxPathError(ValueError):
    """Raised when a requested path escapes the sandbox directory."""


def _resolve(sandbox: Path, raw_path: str) -> Path:
    if raw_path.strip() in {"", ".", "/"}:
        return sandbox
    target = (sandbox / raw_path).resolve()
    if not target.is_relative_to(sandbox):
        raise SandboxPathError(f"path escapes sandbox: {raw_path!r}")
    return target


def make_fs_tools(sandbox_dir: str | Path) -> list:
    """Build read_file / write_file / list_files bound to `sandbox_dir`."""
    sandbox = Path(sandbox_dir).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)

    def write_file(path: str, content: str) -> str:
        """在沙箱内写入文本文件（自动创建目录）。"""
        target = _resolve(sandbox, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入 {target.relative_to(sandbox)}（{len(content)} 字符）"

    def read_file(path: str) -> str:
        """读取沙箱内的文本文件。"""
        target = _resolve(sandbox, path)
        if not target.is_file():
            raise FileNotFoundError(f"沙箱中不存在该文件: {path!r}")
        text = target.read_text(encoding="utf-8")
        return f"--- {target.relative_to(sandbox)}（{len(text)} 字符）---\n{text}"

    def list_files() -> str:
        """列出沙箱内所有文件（相对路径）。"""
        paths = sorted(
            p.relative_to(sandbox).as_posix()
            for p in sandbox.rglob("*")
            if p.is_file()
        )
        if not paths:
            return "(沙箱为空)"
        return "\n".join(f"- {p}" for p in paths)

    return [write_file, read_file, list_files]
