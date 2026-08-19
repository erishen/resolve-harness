"""Scripted demo of the Loop: shows the full agent → tools → agent cycle.

This drives the harness programmatically with several turns and prints the
transcript, demonstrating:
  - tool calling (time / math / memory)
  - long-term memory persistence across turns
  - the loop terminating when the model stops calling tools

Usage:
    uv run python examples/tool_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentpulse import Harness  # noqa: E402


def main() -> None:
    with Harness(verbose=False) as h:
        print(f"model: {h.settings.model}\n")

        turns = [
            "What time is it right now?",
            "Add 12345 and 6789.",
            "Remember my favorite color is navy.",
            "What is my favorite color?",
        ]
        for turn in turns:
            reply = h.run(turn)
            print(f"user : {turn}")
            print(f"agent: {reply}\n")

        print("--- transcript ---")
        for msg in h.transcript:
            print(f"  {msg['role']:<9} {msg['content'][:80]}")
        print(f"\nlong-term memory keys: {[m['key'] for m in h.long_term.search()]}")


if __name__ == "__main__":
    main()
