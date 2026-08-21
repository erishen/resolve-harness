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
    - approvals: human decisions from the approval gate, as
      {call_id: {"action": "approve"|"deny"|"edit", "reason"?, "args"?}};
      consumed (reset) by the tools node after each round.
    """

    messages: Annotated[list[Any], add_messages]
    step: int
    max_steps: int
    approvals: dict[str, Any]
