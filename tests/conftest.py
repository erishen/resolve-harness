"""Shared test isolation: keep per-test data out of the project data/ dir."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_chat_history(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the chat-usage log at a temp DB for every test (create_app() builds
    a default ChatHistoryStore when none is injected). Uses a factory dir that
    is NOT the test's tmp_path — some tests use that same dir as the sandbox,
    so a DB file inside it would show up as a sandbox file."""
    db_dir = tmp_path_factory.mktemp("chat_hist")
    monkeypatch.setattr(
        "resolve_harness.chat_history.default_chat_history_path",
        lambda: db_dir / "chat_history.db",
    )


@pytest.fixture(autouse=True)
def _isolate_runtime_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the developer's local data/config.json out of every test.

    create_app() applies the persisted runtime config (default_model etc.) to
    the injected harness - with a fake router lacking `.settings`, a locally
    set default_model would crash the whole suite. Point the config at a
    nonexistent file so tests see defaults; dedicated config tests monkeypatch
    _config_path themselves and override this."""
    cfg_dir = tmp_path_factory.mktemp("runtime_cfg")
    monkeypatch.setattr(
        "resolve_harness.api._config_path",
        lambda: cfg_dir / "config.json",
    )


@pytest.fixture(autouse=True)
def _isolate_event_log(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the append-only event log out of the real data/event_log.db.

    Both Harness and create_app build EventLog() with no injection path, so they
    fall back to default_event_log_path() — point it at a temp DB for every test
    so turn/task events never pollute (or are read from) the developer's log."""
    log_dir = tmp_path_factory.mktemp("event_log")
    monkeypatch.setattr(
        "resolve_harness.event_log.default_event_log_path",
        lambda: log_dir / "event_log.db",
    )


@pytest.fixture(autouse=True)
def _isolate_task_history(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the task-history DB out of the real data/task_history.db.

    TaskRunner falls back to default_history_path() when no path is injected
    (e.g. create_app() with a bare Harness), so point it at a temp DB — mirroring
    the chat-history / event-log isolation already in place."""
    hist_dir = tmp_path_factory.mktemp("task_hist")
    monkeypatch.setattr(
        "resolve_harness.tasks.default_history_path",
        lambda: hist_dir / "task_history.db",
    )


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's local .env from every test.

    config.py loads .env into os.environ at import time, so tests building a
    real Harness() would otherwise see the developer's LLM_MODEL / LLM_API_BASE
    and behave differently per machine (e.g. _resolve() adds an `openai/`
    prefix only when a custom api_base exists). Tests that need specific env
    values set them explicitly with their own monkeypatch."""
    for var in (
        "LLM_MODEL",
        "LLM_API_BASE",
        "LLM_API_KEY",
        "LLM_TEMPERATURE",
        "LLM_MAX_TOKENS",
        "LLM_MAX_STEPS",
        "HARNESS_VERBOSE",
        "HARNESS_LOG_LEVEL",
        # 开发者 .env 配了 API_TOKEN 会让所有 create_app 测试变 401
        "API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
