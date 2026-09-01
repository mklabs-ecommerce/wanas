"""Turning a stored conversation back into something a person can read.

Shared by the two things that show a conversation to a human rather than to
the model: the local harness (`assistant/harness/web.py`) and the staff
dashboard (`dashboard/web.py`). Neither owns this logic -- it reads
back out of the same neutral history (`assistant/messages.py`) `handle_message`
already wrote, rather than the agent growing a field that exists only so a UI
has something to render.
"""

from __future__ import annotations

#: Channels that tell the shop whether the customer has actually read a
#: message, in a form that can be pinned to one specific message.
#:
#: WhatsApp does: `statuses[]` names the message by the same id the send
#: returned (`assistant/channels/whatsapp.py::_accept_statuses`).
#:
#: Instagram does **not**, and is deliberately absent rather than pending.
#: Its read event is a *watermark* -- "everything up to this timestamp has
#: been read" -- carried on a webhook field the app does not subscribe to, so
#: there is nothing to match against a message and nothing arriving to match.
#: A channel not in here shows no seen state at all, which is the honest
#: reading: "we do not know", never "not seen yet".
RECEIPT_CHANNELS = frozenset({"whatsapp"})


def supports_receipts(channel: str | None) -> bool:
    return channel in RECEIPT_CHANNELS


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

    `by` on an assistant message says which of three voices it is: the
    model's own words (absent), a staff reply written from the dashboard
    (`"staff"`), or an automated push the shop sent on its own -- an order
    confirmation, a shipping update, a back-in-stock notice, a cart nudge
    (`"system"`, `assistant/runtime.py::record_outbound`). They must never
    look identical, or a staff member reading the transcript back cannot tell
    who actually promised the customer something.
    """
    items: list[dict] = []
    for message in history:
        role = message.get("role")
        if role == "user":
            items.append(
                {
                    "kind": "user",
                    "text": message.get("content", ""),
                    # When this message was stored. None for anything written
                    # before messages carried a time at all -- shown as no
                    # time rather than a guessed one.
                    "at": message.get("at"),
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
                        "at": message.get("at"),
                        # "failed" for a message that was composed and stored
                        # but never reached the customer's phone. The UI has
                        # to say so: a staff member reading a thread must not
                        # believe a shipping update landed when Meta refused
                        # it. See `assistant/messages.py::assistant`.
                        "delivery": message.get("delivery"),
                        # How far this message got on the customer's side, as
                        # the platform reported it: sent / delivered / read,
                        # or None when nothing has come back (yet, or ever --
                        # a channel with no receipts, or a message sent before
                        # they were recorded). Only outbound messages carry
                        # one; a customer's own message has no such thing to
                        # know, which is why this key is on this branch only.
                        "receipt": (message.get("receipt") or {}).get("status"),
                        "seen_at": (message.get("receipt") or {}).get("at"),
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
