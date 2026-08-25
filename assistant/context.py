"""What the model is actually sent, as opposed to what is stored.

Two different questions that used to have one answer.

`assistant/session.py` decides what a conversation *is*: the live slice, the
archive behind it, and the caps that bound both. `HISTORY_CAP` used to do
double duty as the answer to "how much does the provider see", and that is
where the bot's memory went. The cap counts **messages**, not exchanges, and a
single customer question costs four to six of them -- the question, an
assistant message carrying tool calls, a `tool_results` message, sometimes a
second round of both, then the reply. Forty messages is therefore seven or
eight exchanges, and the eighth is what pushed "the black Cairokee hoodie" off
the front of the window. From the customer's side the bot simply forgot what
they had been talking about for the last ten minutes.

Raising the cap alone does not fix it, because the bulk is not the
conversation. A `get_products` result is the whole matching catalog; a
`get_variants` result is every variant of a product. Those are working notes
for one reply -- once the reply is written, what matters is the reply, not the
rows it was built from. So the window is split:

* the last `MODEL_CONTEXT_MESSAGES` messages go through **verbatim**, tool
  calls, results, signatures and all -- this is the part the model is
  actively working in, and nothing in it may be reshaped;
* everything before that is **compacted**: the words the customer and the bot
  said to each other are kept, and the tool machinery underneath them is
  dropped.

Nothing here summarises. There is no second model call, no paraphrase, and no
judgement about which product mattered -- a compressed sentence that quietly
loses "black" is worse than a shorter history, and this shop has already paid
for one class of bug where the bot said something nobody chose. Compaction
only ever *removes whole messages*; every sentence that survives is the exact
sentence that was said.

This is a read-only view. It never writes, and the stored transcript keeps the
tool exchanges in full -- `session.transcript()` and the dashboard are
unaffected.
"""

from __future__ import annotations

import logging

from assistant.messages import ASSISTANT, TOOL_RESULTS, USER
from config.settings import settings

log = logging.getLogger("wanas.context")


def _compact(message: dict) -> dict | None:
    """One archived message as the model should still see it, or None to drop.

    Kept deliberately literal: the content string is copied, never rewritten.
    """
    role = message.get("role")
    if role == USER:
        return {"role": USER, "content": message.get("content") or ""}
    if role == ASSISTANT:
        content = message.get("content") or ""
        if not content.strip():
            # An assistant message with no words is a tool call and nothing
            # else. Its results are being dropped in the same pass, so keeping
            # an empty shell of it would leave a call with no answer -- which
            # some providers reject outright and none can use.
            return None
        # `tool_calls` and `signature` go with the results they belong to.
        # `attachments` stays off too: it never reached the provider anyway.
        return {"role": ASSISTANT, "content": content}
    # tool_results, and anything a future message type adds.
    return None


def _first_user(messages: list[dict], start: int = 0) -> int:
    for index in range(start, len(messages)):
        if messages[index].get("role") == USER:
            return index
    return len(messages)


def for_model(
    history: list[dict], *, recent: int | None = None, recall: int | None = None
) -> list[dict]:
    """The provider's view of `history`.

    Returns a new list; `history` is not touched. The caps default to
    settings and are arguments for the same reason `session.trim`'s is: a test
    should be able to state the boundary it is about rather than reach into a
    frozen config object.
    """
    recent_cap = max(0, settings.model_context_messages if recent is None else recent)
    recall_cap = max(0, settings.model_context_recall if recall is None else recall)

    if recent_cap <= 0 or len(history) <= recent_cap:
        return list(history)

    start = len(history) - recent_cap
    # The verbatim window must not open on a `tool_results`: the assistant
    # message that made those calls is behind the boundary and is about to be
    # compacted, and a result with no call is the single most reliable way to
    # have a whole request refused. Walking forward rather than back keeps the
    # rule "everything before the boundary is compacted" true without
    # exception.
    while start < len(history) and history[start].get("role") == TOOL_RESULTS:
        start += 1

    verbatim = list(history[start:])

    older: list[dict] = []
    if recall_cap:
        for message in history[:start]:
            kept = _compact(message)
            if kept is not None:
                older.append(kept)
        if len(older) > recall_cap:
            older = older[len(older) - recall_cap :]
        # Start the recalled part at something the customer said, so the model
        # reads it as a conversation resuming rather than an answer with no
        # question in front of it.
        older = older[_first_user(older) :]

    if not older and not verbatim:
        return list(history)

    log.debug(
        "context: %d stored -> %d recalled + %d verbatim",
        len(history),
        len(older),
        len(verbatim),
    )
    return older + verbatim
