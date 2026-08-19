"""CLI entry point: `agentpulse-chat` runs an interactive REPL."""

from __future__ import annotations

from .harness import Harness


def chat() -> None:
    harness = Harness()
    print(f"agentpulse v{_version()} — model: {harness.settings.model}")
    print("Type your message, or 'exit' / Ctrl-D to quit.\n")
    try:
        while True:
            user_input = input("you> ").strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break
            print(f"agent> {harness.run(user_input)}\n")
    except (KeyboardInterrupt, EOFError):
        print("\nbye")
    finally:
        harness.close()


def _version() -> str:
    from . import __version__

    return __version__
