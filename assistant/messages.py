"""The neutral message format -- the contract between agent and provider.

Three shapes, and only three:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [ToolCall, ...]}
    {"role": "tool_results", "results": [ToolResult, ...]}

A ToolCall is {"id", "name", "arguments", "signature"?}; a ToolResult is
{"id", "name", "content"}.

`signature` exists because some models return an opaque blob with each tool
call that has to be echoed back in the next request or the whole thing is
refused. It is carried through history untouched and means nothing here --
absorbing that kind of difference inside the provider layer is the point of
having one.

The agent only ever speaks this. Adding a provider means writing one class and
changing one config value; no other file changes.

Several keys are **storage-only**: `mids`, `mid_labels`, `images`, `audio`,
`attachments`, `by`, `delivery`, `at` and `receipt`. They are written into history and read
back by the dashboard and the harness, and no provider ever sees them --
every translation layer builds its request from `role`/`content`/`tool_calls`
and ignores what it does not recognise. That is the discipline to follow when
something new has to be *remembered* about a message rather than *said* to the
model: add a key here, document why at its site, and never widen what
`assistant/context.py` sends.
"""

from __future__ import annotations

from common.timeutil import utcnow

USER = "user"
ASSISTANT = "assistant"
TOOL_RESULTS = "tool_results"

#: Meta's delivery receipts for one outbound message, weakest to strongest.
#: A receipt only ever moves forward: WhatsApp can deliver `read` before
#: `delivered` on a fast connection, and out-of-order retries are ordinary, so
#: the stored state is the furthest one reached rather than the last one seen.
RECEIPT_ORDER = ("sent", "delivered", "read")


def now_stamp() -> str:
    """The timestamp put on a message as it is stored.

    ISO-8601, UTC, from the one clock (`common/timeutil.py`). A string rather
    than a datetime because history is a JSON column: whatever goes in has to
    survive a round trip through `json.dumps` unchanged, and an ISO string
    both does that and sorts correctly as text.
    """
    return utcnow().isoformat()


def user(
    text: str,
    *,
    images: list[str] | None = None,
    audio: list[str] | None = None,
    provisional: str | None = None,
    mids: list[str] | None = None,
    at: str | None = None,
) -> dict:
    # `at` is when this message was stored -- for an inbound one, when it
    # reached the process. Not Meta's own `messages[].timestamp`: that is the
    # customer's handset clock, and a phone an hour out would put messages in
    # an order the conversation never happened in. Overridable so a caller
    # that genuinely knows better can say so; nothing does today.
    message: dict = {"role": USER, "content": text, "at": at or now_stamp()}
    if mids:
        # Every platform message id this one message was assembled from -- a
        # debounced batch is several WhatsApp messages joined into one. Stored
        # (never sent to the provider) so that when the customer long-presses
        # "reply" on one of them later, `assistant/quoting.py` can find which
        # message they meant instead of guessing from recency.
        message["mids"] = [m for m in mids if m]
    if provisional:
        # The platform message id this was stored under *on arrival*, before
        # the bot had done anything with it. It exists so the conversation is
        # visible the moment a customer writes, instead of only once a reply
        # has been produced -- a bot that is stuck, paused or crashing used to
        # be indistinguishable from a customer who never wrote at all.
        #
        # `assistant/session.py::drop_provisional` removes it again when the
        # turn it belongs to actually runs and stores the real message. One
        # left behind is not litter: it is a message nobody ever answered.
        message["provisional"] = provisional
    if images:
        # The actual photo(s) the customer sent, kept in history (not sent to
        # the provider -- same reasoning and the same "unknown keys are
        # ignored" translation-layer guarantee as `assistant()`'s
        # `attachments` below) so the dashboard can show the real photo next
        # to its transcribed/described text instead of text alone.
        message["images"] = list(images)
    if audio:
        message["audio"] = list(audio)
    return message


def assistant(
    text: str = "",
    tool_calls: list[dict] | None = None,
    signature: str | None = None,
    attachments: list[str] | None = None,
    by: str | None = None,
    mids: list[str] | None = None,
    delivered: bool = True,
    at: str | None = None,
) -> dict:
    # When the shop said this. Written at store time, which for an agent reply
    # is a moment before the send and for a proactive push is the transaction
    # that decided it -- close enough to "when it happened" for a transcript,
    # and the only clock this process controls.
    #
    # Whether the customer has *read* it is a separate key, `receipt`, and it
    # is never set here: it cannot be known until Meta says so, minutes or
    # hours later. `assistant/session.py::record_receipt` is what writes it,
    # matching Meta's status callback back to this message through `mids`.
    message: dict = {"role": ASSISTANT, "content": text or "", "at": at or now_stamp()}
    if not delivered:
        # This message was composed and stored but never reached the
        # customer's phone -- Meta refused it, or it fell outside the
        # 24-hour customer service window with no approved template to
        # reopen the conversation (`domain/services/notifications.py`).
        # Kept in the transcript rather than dropped, because what the shop
        # meant to say is exactly what the staff member following it up
        # needs to read; flagged, because a dashboard that shows it like any
        # other sent message is how an order update nobody received goes
        # unnoticed. Never sent to the provider, same as `attachments`.
        message["delivery"] = "failed"
    if mids:
        # The ids the *platform* gave the messages this reply went out as
        # (WhatsApp assigns one per send, and one reply can be text plus a
        # picker plus a photo). Stamped on after delivery by
        # `assistant/session.py::attach_outbound_ids`; it is what makes a
        # customer's "reply to this" on something the bot said resolvable at
        # all. Never sent to the provider, same as `attachments`.
        #
        # `mid_labels` rides alongside it, written by the same function: for
        # the ids that went out as a photo, what that photo showed. One reply
        # can be four pictures of four colourways under a single stored
        # message, so the id is the only thing telling them apart -- see
        # `assistant/quoting.py`.
        message["mids"] = [m for m in mids if m]
    if tool_calls:
        message["tool_calls"] = tool_calls
    if signature:
        message["signature"] = signature
    if attachments:
        # Images actually sent with this reply, kept in history (not sent to
        # the provider -- every translation layer ignores unknown keys) so a
        # later turn can tell what the customer has already been shown and
        # never resend it uninvited.
        message["attachments"] = list(attachments)
    if by:
        # Set to "staff" for a reply the dashboard sent on a person's behalf
        # while the conversation was paused. Absent (equivalent to "bot") for
        # everything the model itself said -- also never sent to the
        # provider, same reasoning as `attachments`, but here it is what lets
        # `assistant/display.py` show a staff member's own words as
        # unmistakably theirs rather than the model's.
        message["by"] = by
    return message


def tool_call(call_id: str, name: str, arguments: dict, signature: str | None = None) -> dict:
    call = {"id": call_id, "name": name, "arguments": arguments or {}}
    if signature:
        call["signature"] = signature
    return call


def tool_results(results: list[dict]) -> dict:
    return {"role": TOOL_RESULTS, "results": results}


def tool_result(call_id: str, name: str, content: dict) -> dict:
    return {"id": call_id, "name": name, "content": content}

