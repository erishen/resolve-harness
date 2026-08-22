"""Tests for token-usage extraction/diffing in the LLM router wrapper."""

from __future__ import annotations

from types import SimpleNamespace

from resolve_harness.config import Settings
from resolve_harness.llm import LiteLLMRouter, _extract_usage, usage_diff, _retry_wait
from resolve_harness.llm import RetryPolicy


def _router(model: str, base: str | None, key: str | None = "dummy") -> LiteLLMRouter:
    s = Settings()
    s.model = model
    s.api_base = base
    s.api_key = key
    return LiteLLMRouter(s)


def test_extract_usage_from_usage_object() -> None:
    resp = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    )
    assert _extract_usage(resp) == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }


def test_extract_usage_from_dict_partial() -> None:
    """Some providers omit total_tokens; only present keys are returned."""
    resp = SimpleNamespace(usage={"prompt_tokens": 1, "completion_tokens": 2})
    assert _extract_usage(resp) == {"prompt_tokens": 1, "completion_tokens": 2}


def test_extract_usage_none_when_missing() -> None:
    assert _extract_usage(SimpleNamespace(usage=None)) is None
    assert _extract_usage(SimpleNamespace()) is None


def test_usage_diff_counts_since_baseline() -> None:
    class Router:
        total_usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    baseline = {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}
    assert usage_diff(Router(), baseline) == {
        "prompt_tokens": 60,
        "completion_tokens": 30,
        "total_tokens": 90,
    }


def test_usage_diff_none_when_untracked() -> None:
    class Router:
        pass

    assert usage_diff(Router(), None) is None


def test_ensure_user_trailing_appends_user_after_tool() -> None:
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "content": "r", "tool_call_id": "c1"},
    ]
    out = LiteLLMRouter._ensure_user_trailing(msgs)
    assert out[-1]["role"] == "user"
    # 原列表不被修改（保持不可变语义）
    assert msgs[-1]["role"] == "tool"
    assert len(out) == len(msgs) + 1


def test_ensure_user_trailing_noop_when_not_ending_in_tool() -> None:
    msgs = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]
    assert LiteLLMRouter._ensure_user_trailing(msgs) is msgs


def test_resolve_openrouter_namespace_auto_prefix() -> None:
    # OpenRouter 模型名形如 z-ai/glm-5.2:free，首段是作者命名空间而非
    # litellm provider；api_base 命中 openrouter 时应自动补 openrouter/
    # 前缀，否则会报 "LLM Provider NOT provided"。
    r = _router("z-ai/glm-5.2:free", "https://openrouter.ai/api/v1")
    assert r.resolve_model() == "openrouter/z-ai/glm-5.2:free"


def test_resolve_openrouter_explicit_prefix_kept() -> None:
    # 已显式带合法 provider 前缀的不动，避免重复前缀。
    r = _router("openrouter/z-ai/glm-5.2:free", "https://openrouter.ai/api/v1")
    assert r.resolve_model() == "openrouter/z-ai/glm-5.2:free"
    r2 = _router("openai/gpt-4o", "https://openrouter.ai/api/v1")
    assert r2.resolve_model() == "openai/gpt-4o"


def test_resolve_no_slash_gets_openai_prefix() -> None:
    # 无 '/' 的模型名 + 自定义 base 沿用既有约定补 openai/。
    r = _router("gpt-4o", "https://openrouter.ai/api/v1")
    assert r.resolve_model() == "openai/gpt-4o"


def test_resolve_non_openrouter_base_not_auto_prefixed() -> None:
    # 非 OpenRouter 的自定义网关不在本次自动修复范围，保持原样（沿用既有约定）。
    r = _router("org/mymodel", "https://mygw.example/v1")
    assert r.resolve_model() == "org/mymodel"


def test_resolve_no_base_not_auto_prefixed() -> None:
    r = _router("z-ai/glm-5.2:free", None)
    assert r.resolve_model() == "z-ai/glm-5.2:free"


def _rate_limit_exc(message: str, *, retry_after: float | None = None, response_headers=None):
    """构造一个近似 litellm.RateLimitError 的假异常用于测试退避提取。"""
    exc = RuntimeError(message)
    if retry_after is not None:
        exc.retry_after = retry_after  # type: ignore[attr-defined]
    if response_headers is not None:
        exc.response = SimpleNamespace(headers=response_headers)  # type: ignore[attr-defined]
    return exc


def test_retry_wait_uses_exc_retry_after() -> None:
    exc = _rate_limit_exc("rate limited", retry_after=5)
    assert _retry_wait(exc, 0, RetryPolicy()) == 5.0


def test_retry_wait_uses_retry_after_header() -> None:
    # litellm 把 Retry-After 放在响应头、异常属性为空时也能读到。
    exc = _rate_limit_exc(
        "RateLimitError: ...", response_headers={"Retry-After": "5"}
    )
    assert _retry_wait(exc, 0, RetryPolicy()) == 5.0


def test_retry_wait_falls_back_to_retry_after_seconds_field() -> None:
    # OpenRouter 的 JSON 用 retry_after_seconds 字段，裸正则也能兜住。
    msg = 'RateLimitError: ... "retry_after_seconds":5, "provider_name":"Decart" ...'
    exc = _rate_limit_exc(msg)
    assert _retry_wait(exc, 0, RetryPolicy()) == 5.0


def test_retry_wait_exponential_when_unknown() -> None:
    # 没有任何上游提示时退化为指数退避（backoff_base * 2**attempt）。
    exc = _rate_limit_exc("rate limited")
    assert _retry_wait(exc, 0, RetryPolicy(backoff_base=1.5)) == 1.5
    assert _retry_wait(exc, 1, RetryPolicy(backoff_base=1.5)) == 3.0
