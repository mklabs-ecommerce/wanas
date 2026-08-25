"""Resolving a WhatsApp "reply to this message" against the stored transcript.

Meta tells us which message a customer long-pressed and replied to: it arrives
as `context.id` on the inbound message, and the adapter has always read it
(`assistant/channels/whatsapp.py`). What it could not do was *resolve* it. The
only thing `Pending.annotated_text` could match an id against was the other
messages in the same debounce window, and only the photos and voice notes in
it -- so the two cases that actually happen were both invisible:

* replying to something **the bot** said (a size list, one of three products
  it offered, a price) -- never resolvable at all, because nothing recorded
  the id WhatsApp gave a message the shop sent;
* replying to an **earlier** message of the customer's own, from a turn that
  had already been answered -- outside the batch, so nothing to match.

In both cases the quote was dropped and the reply reached the model as a bare
"the black one please" with no indication of what it was attached to. The
model then did the only thing it could: guess from recency, which is exactly
the hallucinated answer to the wrong message.

The fix is on both sides. Every stored message now carries the platform ids it
was sent or received as (`mids` in `assistant/messages.py`), and this module
looks a quoted id up across the **whole** transcript -- archive included, since
a customer may well reply to something said before the current context window
opened.

The quote is folded into the text of the turn as words, because the neutral
message format has no structured "in reply to" field and inventing one would
mean touching every provider. The exact original sentence is quoted, never a
paraphrase of it.
"""

from __future__ import annotations

import logging

from assistant.messages import ASSISTANT, USER

log = logging.getLogger("wanas.quoting")

#: How much of the quoted message is repeated back. Long enough that a size
#: list or a three-product offer survives whole; short enough that quoting a
#: catalog dump cannot dominate the turn.
QUOTE_CHARS = 300


def _ids(message: dict) -> set[str]:
    """Every platform id this stored message is known by.

    `provisional` is included because a customer's message is written on
    arrival under that key (`runtime.record_inbound`) and only later folded
    into the canonical message that carries `mids` -- a reply that lands in
    between must still resolve.
    """
    found = {m for m in (message.get("mids") or []) if m}
    provisional = message.get("provisional")
    if provisional:
        found.add(provisional)
    return found


def find(transcript: list[dict], target_id: str) -> dict | None:
    """The stored message a quoted platform id refers to, newest first."""
    if not target_id:
        return None
    for message in reversed(transcript):
        if target_id in _ids(message):
            return message
    return None


def _describe(message: dict) -> str | None:
    role = message.get("role")
    if role not in (USER, ASSISTANT):
        return None
    text = (message.get("content") or "").strip()
    if not text:
        # A message with no words -- a bare photo, most often. Say that much
        # rather than quoting nothing: "they replied to the photo you sent" is
        # still a great deal more than the model had before.
        if message.get("attachments") or message.get("images"):
            return "a photo" if role == ASSISTANT else "a photo they sent"
        return None
    if len(text) > QUOTE_CHARS:
        text = text[:QUOTE_CHARS].rstrip() + "…"
    text = text.replace('"', "'")
    speaker = "you said" if role == ASSISTANT else "they said"
    return f'{speaker}: "{text}"'


def annotate(text: str, transcript: list[dict], target_ids) -> str:
    """`text` with a line in front naming the message(s) it is a reply to.

    Unresolvable ids are simply left out -- a reply to something older than
    the stored transcript, or to a message from before ids were recorded, is
    still an ordinary message and must still be answered. Silence about it
    beats inventing which one it was.
    """
    wanted = [t for t in dict.fromkeys(target_ids or []) if t]
    if not wanted:
        return text

    quotes = []
    for target in wanted:
        message = find(transcript, target)
        if message is None:
            log.info("quoted message %s is not in this transcript; answering unannotated", target)
            continue
        described = _describe(message)
        if described:
            quotes.append(described)

    if not quotes:
        return text

    header = "[the customer is replying to an earlier message -- " + "; ".join(quotes) + "]"
    return f"{header}\n{text}" if text.strip() else header
