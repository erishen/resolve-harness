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
        "agentpulse.chat_history.default_chat_history_path",
        lambda: db_dir / "chat_history.db",
    )
