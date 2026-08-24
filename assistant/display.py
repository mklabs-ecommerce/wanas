"""Turning a stored conversation back into something a person can read.

Shared by the two things that show a conversation to a human rather than to
the model: the local harness (`assistant/harness/web.py`) and the staff
dashboard (`dashboard/web.py`). Neither owns this logic -- it reads
back out of the same neutral history (`assistant/messages.py`) `handle_message`
already wrote, rather than the agent growing a field that exists only so a UI
has something to render.
"""

from __future__ import annotations


def turn_detail(history: list[dict]) -> list[dict]:
    """The tool calls and results of the most recent turn.

    Walks backwards to the user message that started the turn, which is
    trimming-proof -- indices shift, the shape does not.
    """
    turn: list[dict] = []
    for message in reversed(history):
        role = message.get("role")
        if role == "user":
            break
        if role == "tool_results":
            for result in message.get("results") or []:
                content = result.get("content") or {}
                turn.append(
                    {
                        "name": result.get("name"),
                        "error": content.get("error"),
                        # The refusal payload matters as much as the code:
                        # `out_of_stock` is only actionable with its
                        # `alternatives`.
                        "content": content,
                    }
                )
    turn.reverse()
    return turn


def display_history(history: list[dict]) -> list[dict]:
    """The stored conversation as bubbles, so a reload is not a fresh start.

    `by` on an assistant message distinguishes a staff reply
    (`assistant/messages.py::assistant(..., by="staff")`, written from the
    dashboard) from the model's own words -- the two must never look
    identical, or a staff member reading the transcript back cannot tell who
    actually promised the customer something.
    """
    items: list[dict] = []
    for message in history:
        role = message.get("role")
        if role == "user":
            items.append(
                {
                    "kind": "user",
                    "text": message.get("content", ""),
                    "images": list(message.get("images") or []),
                    "audio": list(message.get("audio") or []),
                }
            )
        elif role == "assistant":
            if message.get("content"):
                items.append(
                    {
                        "kind": "bot",
                        "text": message["content"],
                        "by": message.get("by") or "bot",
                        "attachments": list(message.get("attachments") or []),
                    }
                )
            if message.get("tool_calls"):
                items.append(
                    {"kind": "tools", "names": [c.get("name") for c in message["tool_calls"]]}
                )
        elif role == "tool_results":
            errors = [
                {"name": r.get("name"), "error": (r.get("content") or {}).get("error")}
                for r in message.get("results") or []
                if (r.get("content") or {}).get("error")
            ]
            if errors:
                items.append({"kind": "refusals", "refusals": errors})
    return items
