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

**Compaction removes; it must never delete more than the cap requires.** The
recalled block is aligned to open on something the customer said, which reads
better than an answer with no question in front of it. That alignment used to
walk *forward* to the first user message and drop everything before it -- and
a conversation the **shop** started has no user message in front of its first
line at all, because a back-in-stock notice, an abandoned-cart nudge and a
status push are all written here by `domain/services/notifications.py` as
assistant messages. The whole recalled block was discarded on that basis,
including the one line saying what the conversation was about, while `recall`
still had fifty free slots. Five exchanges in, the bot could no longer say
what it had messaged the customer about. `_opening` snaps backwards instead,
so alignment can only ever keep more, never less.

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


def _opening(messages: list[dict], cut: int) -> int:
    """Where the recalled block should begin, at or *before* `cut`.

    The block reads better when it opens on something the customer said --
    an answer with no question in front of it is a confusing way to resume.
    This used to be done by walking *forward* to the first user message and
    throwing away everything before it, which is a cosmetic preference
    implemented as unbounded data loss:

    * a conversation the **shop** started -- a back-in-stock notice, an
      abandoned-cart nudge, a status push, all written into `sessions` by
      `domain/services/notifications.py` as assistant messages -- has no user
      message in front of its opening line at all. The whole recalled block
      was therefore discarded, and with it the one message that said what the
      conversation was *about*. Five exchanges in, the bot no longer knew
      what it had nudged the customer about, while `recall` still had fifty
      free slots to hold it in.
    * any run of consecutive assistant messages sitting at the cut was
      dropped for the same reason.

    So it snaps backwards instead. Keeping a few messages more than
    `recall` is the safe direction to be wrong in, and it is the direction
    `session.trim` already chose for the same class of problem ("keeps more
    than the cap" rather than return a fragment). When nothing before `cut`
    is a user message -- the shop-opened case -- the answer is 0: keep the
    block whole rather than delete it.
    """
    for index in range(min(cut, len(messages)), -1, -1):
        if index < len(messages) and messages[index].get("role") == USER:
            return index
    return 0


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
        # Start the recalled part at something the customer said, so the model
        # reads it as a conversation resuming rather than an answer with no
        # question in front of it -- but never by deleting what came before
        # the first thing they said. `_opening` snaps backwards, so a block
        # that is already inside the cap is kept whole.
        if len(older) > recall_cap:
            older = older[_opening(older, len(older) - recall_cap) :]

    if not older and not verbatim:
        return list(history)

    log.debug(
        "context: %d stored -> %d recalled + %d verbatim",
        len(history),
        len(older),
        len(verbatim),
    )
    return older + verbatim
