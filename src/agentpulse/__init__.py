"""agentpulse: a small Python AI agent harness.

Design pillars:
- Loop   — a LangGraph StateGraph: agent -> tools -> agent ... until done.
- Harness — one entry point that wires config, model routing, tools, memory.
- Memory — short-term (session messages) + long-term (SQLite facts).

Public API:
    from agentpulse import Harness
    h = Harness(model="openai/gpt-4o-mini", api_key="...")
    print(h.run("what time is it?"))
"""

from .harness import Harness

__all__ = ["Harness"]
__version__ = "0.1.0"
