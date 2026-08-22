"""Model routing via LiteLLM: one interface, many providers.

The harness never talks to a provider SDK directly — it goes through
`litellm.completion`, which handles OpenAI/Anthropic/DeepSeek/Gemini/... and
any OpenAI-compatible endpoint (custom `api_base`). Switching models is just
a config change.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import litellm

from .config import Settings

logger = logging.getLogger(__name__)

# LiteLLM prints a lot of setup logging by default; keep it quiet unless asked.
litellm.suppress_debug_info = True
litellm.drop_params = True

# keys we track per call (OpenAI-compatible usage shape)
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")

# 速率限制重试：厂商配额（RPM/TPM）耗尽时指数退避后自动重试，让短暂的配额
# 窗口自行恢复，而不是一次失败就中断整轮。
_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_BACKOFF = 1.5  # 秒，按 2 的幂递增：1.5 / 3 / 6 …

# 低配额网关（如 sensenova.cn 这类 RPM 极低的 OpenAI 兼容端点）：默认策略救
# 不回来，自动升级为更多重试 + 更长退避。
_LOW_QUOTA_HOSTS = ("sensenova",)

# 可重试的临时故障：配额耗尽（429）、上游 5xx（如 Cloudflare 520，retryable）、
# 网络超时。这些失败重试通常能自愈；4xx（参数/鉴权错误）绝不重试。
_RETRYABLE_EXC = (
    litellm.RateLimitError,
    litellm.InternalServerError,
    litellm.Timeout,
)


def _retry_wait(exc: Exception, attempt: int, policy: "RetryPolicy") -> float:
    """退避时长：优先采用上游给出的 retry_after（如 Cloudflare 520 的 60s），
    clamp 到 [backoff_base, 15s] 避免单次等待卡死；否则指数退避。"""
    ra = getattr(exc, "retry_after", None)
    if ra is None:
        m = re.search(r"retry_after['\"]?\s*[:=]\s*(\d+)", str(exc))
        ra = int(m.group(1)) if m else None
    try:
        ra = float(ra)
    except (TypeError, ValueError):
        ra = None
    if ra and ra > 0:
        return min(max(ra, policy.backoff_base), 15.0)
    return policy.wait(attempt)


@dataclass
class RetryPolicy:
    """Provider-scoped retry policy（借鉴 dsh `llm-retry`：重试是横切关注点）。

    `retry_on` 列出需要退避重试的异常类型；`complete()` 按当前 api_base
    匹配策略（低配额网关自动升级），其余情况用 router 默认策略。
    """

    max_retries: int = _RATE_LIMIT_MAX_RETRIES
    backoff_base: float = _RATE_LIMIT_BACKOFF
    enabled: bool = True
    retry_on: tuple[type[BaseException], ...] = _RETRYABLE_EXC

    def wait(self, attempt: int) -> float:
        return self.backoff_base * (2 ** attempt)


# 合法环境变量名：字母/下划线开头，仅含字母、数字、下划线。
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LLMError(RuntimeError):
    """Raised when the model call fails after retries."""


def _extract_usage(resp: Any) -> dict[str, int] | None:
    """Pull prompt/completion/total token counts from a litellm response.

    Returns None when the provider omitted usage (some providers/endpoints do).
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        data = usage
    else:  # OpenAI-style Usage object
        data = {k: getattr(usage, k, None) for k in _USAGE_KEYS}
    out: dict[str, int] = {}
    for k in _USAGE_KEYS:
        v = data.get(k)
        if isinstance(v, (int, float)):
            out[k] = int(v)
    return out or None


def usage_snapshot(router: Any) -> dict[str, int] | None:
    """Snapshot a router's cumulative token usage (None if it doesn't track)."""
    total = getattr(router, "total_usage", None)
    if not total:
        return None
    # 并行任务模式下 complete() 在 _usage_lock 下写 total_usage，读也需加锁，
    # 否则可能读到中间状态（RuntimeError: dictionary changed size during iteration）。
    lock = getattr(router, "_usage_lock", None)
    if lock is not None:
        with lock:
            return dict(total)
    return dict(total)


def usage_diff(router: Any, baseline: dict[str, int] | None) -> dict[str, int] | None:
    """Tokens consumed since `baseline` (per-call diff). None when untracked."""
    total = getattr(router, "total_usage", None)
    if not total or not baseline:
        return None
    lock = getattr(router, "_usage_lock", None)
    snap = dict(total) if lock is None else _locked_snapshot(router, lock, total)
    return {
        k: max(0, int(snap.get(k, 0)) - int(baseline.get(k, 0)))
        for k in _USAGE_KEYS
    }


def _locked_snapshot(router: Any, lock: Any, total: dict[str, int]) -> dict[str, int]:
    with lock:
        return dict(total)


class LiteLLMRouter:
    """Thin wrapper around litellm.completion with retry + JSON-ish tool calls."""

    def __init__(
        self,
        settings: Settings,
        *,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.settings = settings
        # .env 基线模型名（LLM_MODEL）。「聊天模型」覆盖（default_model）只改
        # settings.model；任务模型空值时跟随的是这个基线，互不干扰。
        self.env_model = settings.model
        # 默认重试策略；低配额网关由 _policy_for 自动升级。
        self.retry_policy = retry_policy or RetryPolicy()
        # Token usage accounting: last call + cumulative (thread-safe, since
        # task specialists may call the router concurrently).
        self.last_usage: dict[str, int] | None = None
        self.total_usage: dict[str, int] = {k: 0 for k in _USAGE_KEYS}
        self._usage_lock = threading.Lock()
        # Named model profiles: {alias: {base_url, model, api_key_env}}.
        # `complete(model=<alias>)` resolves the profile; api_key is read from
        # the environment variable named by api_key_env (never persisted here).
        self.models: dict[str, dict[str, str]] = {}

    def set_models(self, profiles: dict[str, dict[str, str]]) -> None:
        """Replace the named model profiles (thread-safe by replacement)."""
        self.models = dict(profiles or {})

    def _resolve(self, model: str | None) -> tuple[str, str | None, str | None]:
        """Resolve a model alias to (litellm model, api_base, api_key).

        A profile's ``api_key_env`` accepts *either* a direct secret or an
        environment-variable name (litellm-compatible semantics):
          - empty            -> fall back to the global .env default (settings.api_key)
          - "OPENAI_API_KEY" -> resolved from os.environ when that var is set
          - a raw key        -> used verbatim
        """
        name = model or self.settings.model
        profile = (self.models or {}).get(name)
        if not profile:
            model_name, base, key = name, self.settings.api_base, self.settings.api_key
        else:
            base = profile.get("base_url") or self.settings.api_base
            spec = (profile.get("api_key_env") or "").strip()
            if not spec:
                key = self.settings.api_key
            elif _ENV_NAME_RE.match(spec) and os.environ.get(spec):
                key = os.environ.get(spec)
            else:
                # 直接密钥；填了 env 名却未在 .env 设置时也按字面密钥使用。
                key = spec
            model_name = profile.get("model") or name
        # 自定义 OpenAI 兼容网关（base_url 非空）下，litellm 需要明确的
        # provider 前缀；模型名未带前缀（无 '/'）时默认补 `openai/`，否则会报
        # "LLM Provider NOT provided"。已带前缀（如 openai/、deepseek/）原样。
        if base and "/" not in model_name:
            model_name = "openai/" + model_name
        return model_name, base, key

    def _policy_for(self, api_base: str | None) -> RetryPolicy:
        """按 api_base 选择重试策略：低配额网关自动升级（更多重试、更长退避），
        其余情况用 router 默认策略。"""
        base = (api_base or "").lower()
        if any(host in base for host in _LOW_QUOTA_HOSTS):
            return RetryPolicy(max_retries=4, backoff_base=2.0)
        return self.retry_policy

    # -- public API ------------------------------------------------------

    @staticmethod
    def _ensure_user_trailing(
        messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """OpenAI 兼容网关（agnes 等）不允许对话以 `tool` 消息结尾，否则报
        'No user query found in messages'。结尾是 tool 时补一条 user 消息，
        让请求落在 user 轮次上。标准 OpenAI / Anthropic / DeepSeek 走官方
        前缀、无自定义 base，不会触发此垫片。"""
        if messages and messages[-1].get("role") == "tool":
            return [*messages, {"role": "user", "content": "（请基于上面的工具返回继续）"}]
        return messages

    def resolve_model(self, model: str | None = None) -> str:
        """Resolve a model alias (or the router default) to the concrete
        litellm model name — exactly the same resolution `complete()` uses.

        Used by the UI/task records to display the model that will actually
        be called (alias -> profile model, with provider-prefix fallback).
        """
        return self._resolve(model)[0]

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Call the model. Returns the raw message dict from the provider.

        The message dict follows the OpenAI shape:
            {"role": "assistant", "content": ..., "tool_calls": [...]}
        `model` overrides the router default per call — either a profile alias
        (resolved via set_models) or a raw litellm model string.
        """
        model_name, api_base, api_key = self._resolve(model)
        policy = self._policy_for(api_base)
        out_messages = [dict(m) for m in messages]
        # 自定义兼容网关（agnes 等）拒绝以 tool 结尾的请求，补一条 user 收尾。
        if api_base:
            out_messages = self._ensure_user_trailing(out_messages)
        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": out_messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": self.settings.max_tokens if max_tokens is None else max_tokens,
        }
        if api_base:
            kwargs["api_base"] = api_base
        if api_key:
            kwargs["api_key"] = api_key
        if tools:
            kwargs["tools"] = tools
        # 关掉 litellm 自带重试，改由下方统一退避重试，避免双重退避叠加等待。
        kwargs["num_retries"] = 0

        last_exc: Exception | None = None
        for attempt in range(policy.max_retries + 1):
            try:
                resp = litellm.completion(**kwargs)
                break
            except policy.retry_on as exc:  # 配额/5xx/超时：退避后重试
                last_exc = exc
                if attempt < policy.max_retries:
                    wait = _retry_wait(exc, attempt, policy)
                    logger.warning(
                        "LLM 调用失败（%s，第 %d/%d 次），%.1fs 后重试：%s",
                        model_name, attempt + 1, policy.max_retries, wait, exc,
                    )
                    time.sleep(wait)
                    continue
                raise LLMError(
                    f"LLM call failed ({model_name}): 重试耗尽 - {exc}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - surface other provider errors uniformly
                raise LLMError(f"LLM call failed ({model_name}): {exc}") from exc
        else:  # 不应到达（循环内已 break 或 raise），兜底
            raise LLMError(f"LLM call failed ({model_name}): {last_exc}") from last_exc

        message = resp.choices[0].message
        raw: dict[str, Any] = {"role": "assistant", "content": message.content}
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            raw["tool_calls"] = [
                {
                    "id": tc.id or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for i, tc in enumerate(tool_calls)
            ]

        usage = _extract_usage(resp)
        if usage:
            with self._usage_lock:
                self.last_usage = dict(usage)
                for k, v in usage.items():
                    self.total_usage[k] = self.total_usage.get(k, 0) + v
        return raw

    def parse_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize provider tool_calls into [{id, name, arguments(dict)}]."""
        calls: list[dict[str, Any]] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.get("id", ""), "name": name, "arguments": args})
        return calls
