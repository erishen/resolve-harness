"""Interactive REPL chat with the agentpulse harness.

Usage:
    uv run python examples/chat.py

Requires LLM_MODEL / LLM_API_KEY (or .env) — see .env.example.
"""

from __future__ import annotations

import sys
from pathlib import Path

# allow `uv run python examples/chat.py` without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentpulse import Harness  # noqa: E402


def main() -> None:
    harness = Harness()
    print(f"agentpulse v0.1.0 — model: {harness.settings.model}")
    print("Type your message, or 'exit' / Ctrl-D to quit.\n")

    try:
        while True:
            user_input = input("you> ").strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break
            reply = harness.run(user_input)
            print(f"agent> {reply}\n")
    except (KeyboardInterrupt, EOFError):
        print("\nbye")
    finally:
        harness.close()


if __name__ == "__main__":
    main()
