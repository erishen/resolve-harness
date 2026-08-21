"""Tests for token-usage extraction/diffing in the LLM router wrapper."""

from __future__ import annotations

from types import SimpleNamespace

from agentpulse.llm import _extract_usage, usage_diff


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
