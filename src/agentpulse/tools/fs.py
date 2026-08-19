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
        """Write text content to a file inside the sandbox (creates dirs)."""
        target = _resolve(sandbox, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"written {target.relative_to(sandbox)} ({len(content)} chars)"

    def read_file(path: str) -> str:
        """Read a text file from inside the sandbox."""
        target = _resolve(sandbox, path)
        if not target.is_file():
            raise FileNotFoundError(f"no such file in sandbox: {path!r}")
        text = target.read_text(encoding="utf-8")
        return f"--- {target.relative_to(sandbox)} ({len(text)} chars) ---\n{text}"

    def list_files() -> str:
        """List all files currently in the sandbox (relative paths)."""
        paths = sorted(
            p.relative_to(sandbox).as_posix()
            for p in sandbox.rglob("*")
            if p.is_file()
        )
        if not paths:
            return "(sandbox is empty)"
        return "\n".join(f"- {p}" for p in paths)

    return [write_file, read_file, list_files]
