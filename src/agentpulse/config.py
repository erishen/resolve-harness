"""Configuration layer: reads .env / environment variables into a Settings dataclass.

All knobs have sane defaults so the harness runs out of the box; secrets
(API keys) are only ever read from the environment, never hard-coded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# litellm tries to fetch its model-cost map from GitHub at import time, which
# can stall server startup by ~15s on slow/flaky networks (SSL timeout).
# Prefer the bundled local copy — must be set before `import litellm`.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def _find_project_root() -> Path:
    """Walk up from this file to the directory containing pyproject.toml."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[2]


def _load_env_files() -> None:
    """Load .env from project root first, then from CWD (allows overrides)."""
    root = _find_project_root()
    load_dotenv(root / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)


_load_env_files()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass
class Settings:
    """Runtime configuration for the harness.

    Attributes:
        model: LiteLLM model string, e.g. "openai/gpt-4o-mini",
            "deepseek/deepseek-chat", "anthropic/claude-3-5-sonnet-latest".
        api_base: optional OpenAI-compatible base URL (LiteLLM passes it
            through as `api_base`).
        api_key: provider API key. Defaults to the env var matching the
            provider (OPENAI_API_KEY etc.), so you usually don't set it.
        temperature: sampling temperature.
        max_tokens: cap for a single LLM completion.
        max_steps: hard cap for agent loop iterations (anti-infinite-loop).
        system_prompt: default system prompt, mentions memory/tool usage.
        verbose: print trace of loop steps to stderr.
    """

    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "openai/gpt-4o-mini"))
    api_base: str | None = field(
        default_factory=lambda: os.getenv("LLM_API_BASE") or None
    )
    api_key: str | None = field(
        default_factory=lambda: os.getenv("LLM_API_KEY") or None
    )
    temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.7))
    max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 2048))
    max_steps: int = field(default_factory=lambda: _env_int("LLM_MAX_STEPS", 10))
    system_prompt: str = field(
        default_factory=lambda: os.getenv(
            "LLM_SYSTEM_PROMPT",
            (
                "You are a helpful AI assistant running inside an agent harness. "
                "You have access to tools: call them when you need facts, "
                "computations, or persisted memory. Think step by step, but be "
                "concise. When a user asks a question you can answer directly, "
                "answer directly."
            ),
        )
    )
    verbose: bool = field(default_factory=lambda: os.getenv("HARNESS_VERBOSE", "0") == "1")

    def __post_init__(self) -> None:
        if self.api_base and not self.api_base.endswith("/"):
            self.api_base = self.api_base + "/"
