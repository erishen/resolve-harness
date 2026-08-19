"""LangGraph state definition for the agent loop."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """The mutable state threaded through every loop iteration.

    - messages: the running transcript. `add_messages` is LangGraph's
      reducer — new messages are appended and tool responses are matched
      back to their calls by id.
    - step: current iteration counter (0-based before first agent call).
    - max_steps: hard ceiling; the harness refuses to loop past it, which
      is the anti-runaway guarantee.
    """

    messages: Annotated[list[Any], add_messages]
    step: int
    max_steps: int
