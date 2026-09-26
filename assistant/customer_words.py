"""What the customer actually wrote, with everything the runtime added taken out.

A stored customer message is not only the customer's words. The runtime folds
notes into it for the model to read: the automated reading of a photo they
sent («[الزبون بعت صورة] قراءة آلية للصورة: ... المقاسات ...»), the message a
long-pressed reply quoted -- often the bot's own sentence -- the label of a
comment or a story, the type of an attachment nobody can read.

Several rules below the model are decided "from the customer's own words":
whether they asked about sizing, whether they asked to see photos, whether
they asked for every colour. Production, 2026-09-25, Instagram: the photo note
says «...السعر أو المقاسات أو التوفر» and «صورة», so a customer who had sent
nothing but a picture was read as asking about sizing *and* for photos -- and
the size chart went out between the product photos three times. Every one of
those rules reads through here now.
"""

from __future__ import annotations

import re

#: `assistant/media.py::photo_context` -- the marker and the reading after it,
#: which runs to the end of the line.
_PHOTO_NOTE = re.compile(r"\[الزبون بعت صورة\][^\n]*")

#: `assistant/quoting.py::annotate` -- the quoted message, which may be the
#: bot's own words and may span lines, then the customer's text on the next.
_QUOTE_HEADER = re.compile(
    r"\[the customer is replying to an earlier message -- .*?\](?=\n|$)", re.DOTALL
)

#: `assistant/dispatcher.py::Pending.annotated_text`.
_BATCH_REPLY = re.compile(r"\[replying to [^\]\n]*\]")

#: Instagram's labels (`channels/instagram.py`): a comment's post, a story,
#: and the bare type of an attachment the bot could not read («[template]»).
_LABELS = re.compile(
    r"\[كومنت على بوست [^\]\n]*\]|\[رد على ستوري\]|\[الزبون منشنك في ستوري\]|^\[[a-z_]+\]$",
    re.MULTILINE,
)


def own_words(content: str | None) -> str:
    """`content` with the runtime's notes removed, whitespace tidied."""
    text = content or ""
    for pattern in (_QUOTE_HEADER, _PHOTO_NOTE, _BATCH_REPLY, _LABELS):
        text = pattern.sub(" ", text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def recent(history: list[dict], count: int = 1) -> list[str]:
    """The customer's last `count` messages that said anything of their own,
    newest first, each through `own_words`.

    A message that was nothing but a note (a bare photo) is skipped rather
    than returned empty: the question "what did they ask?" is about the last
    thing they *said*.
    """
    found: list[str] = []
    for message in reversed(history):
        if message.get("role") != "user":
            continue
        words = own_words(message.get("content"))
        if words:
            found.append(words)
        if len(found) >= count:
            break
    return found


def latest(history: list[dict]) -> str:
    """The customer's own words in their most recent message, or "".

    Only the most recent message: a bare photo sent after a question about
    sizes is a new turn, and the older question is not what it asks.
    """
    for message in reversed(history):
        if message.get("role") == "user":
            return own_words(message.get("content"))
    return ""
