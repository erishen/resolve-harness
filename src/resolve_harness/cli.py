"""CLI entry point: `resolve_harness-chat` runs an interactive REPL."""

from __future__ import annotations

import json

from .harness import Harness


def chat() -> None:
    harness = Harness()
    print(f"resolve_harness v{_version()} — model: {harness.settings.model}")
    print("Type your message, or 'exit' / Ctrl-D to quit.\n")
    try:
        while True:
            user_input = input("you> ").strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break
            try:
                reply = harness.run(user_input)
            except KeyboardInterrupt:  # allow Ctrl-C to bail out of a long turn
                print("\n(aborted turn)")
                continue
            reply = _resolve_approvals(harness, reply)
            print(f"agent> {reply}\n")
    except (KeyboardInterrupt, EOFError):
        print("\nbye")
    finally:
        harness.close()


def _resolve_approvals(harness: Harness, reply: str) -> str:
    """Loop on any pending human-in-the-loop approval until the turn finishes.

    Each pending gate lists the tool calls awaiting a decision; the operator
    approves / denies / edits them, and the resumed turn may suspend again on
    a later flagged call (chained approval).
    """
    while harness.pending_approval is not None:
        pending = harness.pending_approval
        print("\n需要人工审批（以下工具调用执行前需确认）：")
        for call in pending["tool_calls"]:
            args = json.dumps(call["args"], ensure_ascii=False)
            print(f"  • {call['name']}({args})  [id={call['id']}]")

        choice = input("\n[y] 全部批准  [n] 全部拒绝  [e] 逐个改参数 > ").strip().lower()
        if choice == "y":
            decisions: object = "approve"
        elif choice == "n":
            decisions = "deny"
        elif choice == "e":
            decisions = _collect_edits(pending["tool_calls"])
        else:
            print("（无效选择，请重新决定）")
            continue

        try:
            reply = harness.resolve_approval(decisions)
        except Exception as exc:  # noqa: BLE001 - surface errors, re-prompt
            print(f"审批处理出错：{exc}")
            break
    return reply


def _collect_edits(calls: list[dict[str, object]]) -> list[dict[str, object]]:
    """Prompt for an action + (optional) edited args for each pending call."""
    decisions: list[dict[str, object]] = []
    for call in calls:
        call_id = str(call["id"])
        name = str(call["name"])
        args = call.get("args") or {}
        while True:
            sub = input(f"  [{name}] [y]批准 / [n]拒绝 / [e]改参数 > ").strip().lower()
            if sub == "y":
                decisions.append({"id": call_id, "action": "approve"})
                break
            if sub == "n":
                reason = input("    拒绝原因（可选）> ").strip() or None
                decisions.append({"id": call_id, "action": "deny", "reason": reason})
                break
            if sub == "e":
                current = json.dumps(args, ensure_ascii=False)
                raw = input(f"    新参数 JSON（当前 {current}）> ").strip()
                try:
                    new_args = json.loads(raw) if raw else args
                except json.JSONDecodeError:
                    print("    （参数不是合法 JSON，请重试）")
                    continue
                decisions.append({"id": call_id, "action": "edit", "args": new_args})
                break
            print("    （无效选择）")
    return decisions


def _version() -> str:
    from . import __version__

    return __version__
