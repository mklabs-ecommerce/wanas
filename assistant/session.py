"""Conversation history, in the database, keyed by channel + external_id.

Not in process memory: the server has to restart without customers losing
their conversation, and more than one instance has to be able to run behind a
load balancer.

Losing a session loses nothing real *to the bot* -- the cart is stored
separately and survives. It loses something real to the shop, though: this
table is also the only record of what a customer and the bot actually said to
each other, and it is what the dashboard reads. So nothing here deletes a
message. Ending a conversation -- six hours of silence, a staff reset, or
history scrolling past `HISTORY_CAP` -- moves `SessionRow.context_start`
forward instead: the model sees a fresh conversation, the transcript keeps
everything. `transcript()` is what reads the whole thing back.

The stored transcript is bounded (`SESSION_ARCHIVE_CAP`) so one row cannot
grow without limit; passing that bound is the one place a message is dropped,
and it is logged.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from assistant.messages import ASSISTANT, RECEIPT_ORDER, USER
from common.timeutil import as_aware
from config.settings import settings
from domain.models import UNREADABLE_HISTORY, SessionRow, utcnow

log = logging.getLogger("wanas.session")


def trim(history: list[dict], cap: int | None = None) -> list[dict]:
    """Drop old messages, but only ever cut at a user message.

    Cutting between a tool call and its result leaves the history malformed and
    providers reject the entire request -- this is the single easiest thing in
    the system to get wrong and the hardest to diagnose. Because a
    tool_results message always immediately follows the assistant message that
    produced it, and both always follow a user message, starting the kept
    history at a user message makes splitting a pair impossible by
    construction.
    """
    cap = settings.history_cap if cap is None else cap
    if cap <= 0 or len(history) <= cap:
        return list(history)

    first_kept = len(history) - cap
    for index in range(first_kept, len(history)):
        if history[index].get("role") == USER:
            return history[index:]

    # The tail holds no user message at all (a long tool exchange). Fall back
    # to the last user message anywhere rather than returning a fragment that
    # starts mid tool-call, even though that keeps more than the cap.
    for index in range(len(history) - 1, -1, -1):
        if history[index].get("role") == USER:
            return history[index:]
    return []


def _expired(row: SessionRow) -> bool:
    if row.updated_at is None:
        return False
    return utcnow() - as_aware(row.updated_at) > timedelta(
        hours=settings.session_expiry_hours
    )


def _stored(row: SessionRow) -> list[dict]:
    """The full transcript, or `[]` if the column cannot be read at all."""
    history = row.history
    if history is UNREADABLE_HISTORY or not isinstance(history, list):
        log.error(
            "session %s/%s has unreadable history (%s); this turn starts empty "
            "(stored value left in place until the next save)",
            row.channel,
            row.external_id,
            type(history).__name__,
        )
        return []
    return history


def _start(row: SessionRow, stored: list[dict]) -> int:
    """`context_start`, clamped -- an old row has none, a bad one may be past
    the end, and neither is a reason to lose the transcript."""
    start = row.context_start or 0
    return max(0, min(start, len(stored)))


def transcript(session: Session, channel: str, external_id: str) -> list[dict]:
    """Everything ever said, archive included, and *read-only*.

    What the dashboard shows. `load` is for the agent and moves the bookmark;
    a staff member opening a conversation must never be what ends it.
    """
    row = session.get(SessionRow, (channel, external_id))
    return list(_stored(row)) if row is not None else []


def archive_boundary(session: Session, channel: str, external_id: str) -> int:
    """How many messages of `transcript()` are archive rather than live."""
    row = session.get(SessionRow, (channel, external_id))
    return _start(row, _stored(row)) if row is not None else 0


def load(session: Session, channel: str, external_id: str) -> list[dict]:
    """The live history for this identity, or a fresh one after 6 hours.

    Expiry archives, it does not erase: `context_start` jumps to the end of
    the stored transcript and the same rows stay in the column. Before this,
    the wipe here (`row.history = []`) was silently destroying every
    conversation that went quiet for six hours -- including when the dashboard
    called `load` to *display* one.

    A row written outside the app -- a manual edit or a restore -- can hold
    text that is not JSON at all. The decode happens while the row is loaded,
    inside `session.get`, and is guarded there (`LenientJSON`): a poisoned
    column arrives here as `UNREADABLE_HISTORY` instead of raising on every
    turn. Answer with an empty history and leave the stored value untouched.
    """
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        return []
    stored = _stored(row)
    if _expired(row):
        start = _start(row, stored)
        if start < len(stored):
            log.info(
                "session %s/%s went idle for over %sh; archiving %d message(s) and "
                "starting a fresh context (nothing deleted)",
                channel,
                external_id,
                settings.session_expiry_hours,
                len(stored) - start,
            )
        row.context_start = len(stored)
        row.updated_at = utcnow()
        session.flush()
        return []
    return list(stored[_start(row, stored) :])


def stored_length(session: Session, channel: str, external_id: str) -> int:
    """How many messages the stored transcript holds right now.

    The bookmark a turn takes before it starts working from an in-memory copy
    of the history, so `save(..., merge_since=...)` can tell what was written
    to the row while the turn was running. See `save`.
    """
    row = session.get(SessionRow, (channel, external_id))
    return len(_stored(row)) if row is not None else 0


def _merge_interleaved(history: list[dict], stored: list[dict], since: int) -> list[dict]:
    """Fold back the messages someone else wrote to the row mid-turn.

    An agent turn reads the history once, keeps it in memory for the whole
    tool loop, and writes it back at the end. Anything appended to the *row*
    in between -- and the order confirmation is exactly that: `place_order`
    records it through `notifications._record` on the turn's own session while
    the model is still composing its reply -- was therefore overwritten by the
    turn's stale copy. The customer had the message on their phone and the
    dashboard did not have it at all, which is precisely the mismatch
    `record_outbound` exists to prevent.

    Chronology, not position: those messages left the shop before the reply
    the turn is about to store, so they go in ahead of it.
    """
    extra = [m for m in stored[since:] if m not in history]
    if not extra:
        return history
    log.info(
        "folding %d message(s) written mid-turn back into the transcript", len(extra)
    )
    tail = 0
    if history and history[-1].get("role") == ASSISTANT and not history[-1].get("tool_calls"):
        tail = 1
    cut = len(history) - tail
    return history[:cut] + extra + history[cut:]


def save(
    session: Session,
    channel: str,
    external_id: str,
    history: list[dict],
    *,
    merge_since: int | None = None,
) -> list[dict]:
    """Store `history` as the live conversation, keeping what came before it.

    `trim` caps what the model is sent; the messages it drops move into the
    archive rather than off the end of the world. Only `SESSION_ARCHIVE_CAP`
    bounds the row, and it says so when it bites.

    `merge_since` is the `stored_length` the caller's copy of the history was
    read at. Given one, anything written to the row since then is folded back
    in rather than overwritten -- see `_merge_interleaved`.
    """
    if merge_since is not None:
        row = session.get(SessionRow, (channel, external_id))
        if row is not None:
            history = _merge_interleaved(history, _stored(row), merge_since)

    trimmed = trim(history)
    dropped = history[: len(history) - len(trimmed)]

    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        row = SessionRow(channel=channel, external_id=external_id, history=[])
        session.add(row)
        archive: list[dict] = []
    else:
        stored = _stored(row)
        archive = list(stored[: _start(row, stored)])

    archive.extend(dropped)
    full = archive + list(trimmed)

    cap = settings.session_archive_cap
    if cap > 0 and len(full) > cap:
        overflow = len(full) - cap
        log.warning(
            "session %s/%s reached the %d-message archive cap; dropping the "
            "oldest %d message(s)",
            channel,
            external_id,
            cap,
            overflow,
        )
        full = full[overflow:]
        archive = archive[overflow:] if overflow < len(archive) else []

    row.history = full
    row.context_start = len(full) - len(trimmed)
    row.updated_at = utcnow()
    session.flush()
    return trimmed


def _rewrite(
    session: Session,
    row: SessionRow,
    stored: list[dict],
    index: int,
    changes: dict,
    *,
    touch: bool = True,
) -> None:
    """Replace one stored message with an annotated copy of itself.

    A brand-new list, and the loaded one left untouched. Mutating it in place
    and then reassigning writes nothing: the column compares the new value
    against the loaded one by equality, and an in-place edit has already
    changed both.

    `touch=False` leaves `updated_at` alone, and it matters more than it
    looks: that column is both the inbox's sort key and the clock the
    six-hour conversation expiry runs on (`_expired`). Something that happens
    *to* an old message rather than being a new one -- a read receipt -- must
    not move either. Bumping it would quietly extend the bot's context window
    every time a customer opened the chat without saying anything, and float
    a silent conversation to the top of the inbox as though it had spoken.
    """
    row.history = [
        {**message, **changes} if i == index else message
        for i, message in enumerate(stored)
    ]
    if touch:
        row.updated_at = utcnow()
    session.flush()


def mark_undelivered(session: Session, channel: str, external_id: str, text: str) -> bool:
    """Flag the newest copy of `text` as a message that never arrived.

    An update to the line already written, not a second line: the transcript
    must read as one message the customer did not get, never as the same
    sentence said twice. Used when the send failure is only learned about
    after the line was recorded -- which is every send, since the message is
    written inside the transaction that decided it and only leaves once that
    transaction has committed.

    False means there was nothing to correct (no recorder wired, a different
    wording), and the caller writes a fresh flagged line instead.
    """
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        return False
    stored = _stored(row)
    for index in range(len(stored) - 1, -1, -1):
        message = stored[index]
        if message.get("role") == ASSISTANT and message.get("content") == text:
            if message.get("delivery") == "failed":
                return True
            _rewrite(session, row, stored, index, {"delivery": "failed"})
            return True
    return False


def attach_ids_to_text(
    session: Session, channel: str, external_id: str, text: str, message_ids: list[str]
) -> bool:
    """Stamp the platform ids a *proactive* message went out as onto its line.

    `attach_outbound_ids` below does this for an agent reply, where "the
    newest assistant message" is unambiguous. A proactive push cannot use
    that: the order confirmation is written inside the order's transaction and
    sent after the commit, and by then the customer may already have written
    again. So it is matched by its own text instead -- the same handle
    `mark_undelivered` uses, and the only one that survives the gap between
    deciding a message and learning what Meta called it.

    Without this, an order confirmation could never be matched to a read
    receipt and would read as permanently unseen -- which is exactly the
    message the shop most wants to know landed.
    """
    ids = [m for m in (message_ids or []) if m]
    if not ids:
        return False
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        return False
    stored = _stored(row)
    for index in range(len(stored) - 1, -1, -1):
        message = stored[index]
        if message.get("role") == ASSISTANT and message.get("content") == text:
            merged = list(dict.fromkeys([*(message.get("mids") or []), *ids]))
            if merged == list(message.get("mids") or []):
                return True
            _rewrite(session, row, stored, index, {"mids": merged})
            return True
    return False


def locate_mid(session: Session, channel: str, message_id: str) -> str | None:
    """Which conversation on `channel` holds the message with this platform id.

    A fallback, not the main road: a receipt names the customer it is about
    (`statuses[].recipient_id`), and that is normally the very `external_id`
    the conversation is keyed on. It stops being so the moment Meta reports a
    recipient under one of its two identifier schemes while the thread was
    opened under the other -- a phone number against a business-scoped user id
    (`common/identifiers.py`) -- and a receipt that cannot find its message is
    an outbound message that reads as never seen forever.

    A platform message id is globally unique, so a scan cannot match the wrong
    conversation; it can only be slow, and it runs only when the direct
    lookup has already missed.
    """
    if not message_id:
        return None
    rows = session.scalars(select(SessionRow).where(SessionRow.channel == channel)).all()
    for row in rows:
        for message in _stored(row):
            if message.get("role") == ASSISTANT and message_id in (message.get("mids") or []):
                return row.external_id
    return None


def record_receipt(
    session: Session,
    channel: str,
    external_id: str,
    message_id: str,
    status: str,
    at: str | None = None,
) -> bool:
    """Record what the platform says happened to one message we sent.

    Matched to a stored message through `mids` -- the same ids
    `assistant/quoting.py` resolves a customer's "reply to this" against, and
    the only link there is between an event on Meta's side and a row in
    `SessionRow.history`.

    One stored message can have gone out as several sends (the words, then a
    picker, then a photo), so a receipt arrives per part. The message keeps
    the furthest state any of its parts reached: a customer whose phone
    confirmed reading any part of a reply has had that reply on screen, and
    tracking each part separately would put a half-read tick on something the
    customer plainly saw.

    Receipts never move backwards (`RECEIPT_ORDER`) -- Meta can deliver `read`
    before `delivered`, and retries arrive out of order. `failed` is not a
    receipt at all: it is Meta telling us, after accepting the send, that the
    message never landed, so it sets the same `delivery` flag a refused send
    does and the dashboard stops showing it as received.

    False means no stored message carries that id -- an ordinary outcome for
    a receipt about something sent before this existed.
    """
    if not message_id or not status:
        return False
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        return False
    stored = _stored(row)
    for index in range(len(stored) - 1, -1, -1):
        message = stored[index]
        if message.get("role") != ASSISTANT or message_id not in (message.get("mids") or []):
            continue

        if status == "failed":
            if message.get("delivery") == "failed":
                return True
            _rewrite(session, row, stored, index, {"delivery": "failed"}, touch=False)
            return True

        if status not in RECEIPT_ORDER:
            # An unknown status is not something to guess at. Named, so a new
            # one Meta starts sending is visible rather than silently dropped.
            log.info("ignoring unknown whatsapp status %r for %s", status, message_id)
            return False

        current = (message.get("receipt") or {}).get("status")
        if current in RECEIPT_ORDER and RECEIPT_ORDER.index(current) >= RECEIPT_ORDER.index(status):
            return True
        _rewrite(
            session,
            row,
            stored,
            index,
            {"receipt": {"status": status, "at": at or utcnow().isoformat()}},
            touch=False,
        )
        return True
    return False


def drop_provisional(history: list[dict], recorded_ids: set[str] | None) -> list[dict]:
    """Remove the on-arrival copies of the messages a turn is about to store.

    A customer's message is written to this table the instant it arrives
    (`assistant/runtime.py::record_inbound`), so the dashboard shows the
    conversation before the bot has replied -- or when it never replies at
    all. When the turn for that message finally runs it stores the message
    properly, with the photo context and reply-to annotations the model
    actually saw, so the provisional copy has to go or the transcript reads as
    if the customer said everything twice.

    Filtered by id rather than by position: a provisional message from an
    *earlier* batch that crashed is deliberately left where it is. That row is
    the only evidence the customer wrote and got nothing back.
    """
    if not recorded_ids:
        return list(history)
    return [
        message
        for message in history
        if not (
            message.get("role") == USER and message.get("provisional") in recorded_ids
        )
    ]


def append(
    session: Session,
    channel: str,
    external_id: str,
    *messages: dict,
    recorded_ids: set[str] | None = None,
) -> list[dict]:
    history = drop_provisional(load(session, channel, external_id), recorded_ids)
    return save(session, channel, external_id, history + list(messages))


def photo_mid_labels(outcomes: list, attachment_labels: dict[str, str]) -> dict[str, str]:
    """Which id was which photo, for the ids that went out as one.

    Each image is its own send with its own id, and `OutboundMessage` carries
    the path it was for -- so the id and the colourway can be paired here
    without counting positions or assuming the attachments went out in order.
    A photo the turn had no label for is simply left out: an unnamed picture
    resolves to its message like any other, which is what used to happen to
    all of them.
    """
    labels: dict[str, str] = {}
    for out in outcomes:
        if not out.delivered:
            continue
        label = attachment_labels.get(out.image_path or "")
        if not label:
            continue
        for mid in out.message_ids:
            labels[mid] = label
    return labels


def attach_outbound_ids(
    session: Session,
    channel: str,
    external_id: str,
    message_ids: list[str],
    mid_labels: dict[str, str] | None = None,
) -> bool:
    """Record the platform ids a reply actually went out as.

    WhatsApp assigns an id to every message *it* accepts, and one agent reply
    can leave as several -- the words, then a picker, then a photo. Those ids
    are the only handle a customer's later "reply to this" has on something
    the bot said, and until this existed nothing kept them: the response body
    was read for a status code and thrown away, so a quoted bot message could
    never be resolved and the model was left guessing which of its own
    sentences the customer meant.

    Stamped onto the newest assistant message rather than passed into the turn
    because the ids do not exist until after the send, which is after the turn
    has already stored its reply. Returns False when there was nothing to
    stamp -- a paused conversation, or a send that failed outright.

    `mid_labels` says what the id at each key *was*, for the ids that went out
    as a photo: "Ringer Boxy Fit Tshirt (Beige)". Knowing the message is not
    enough when one message was four photos, one per colourway -- resolving
    the quote to the message alone hands the next turn the same four colours
    the customer was trying to choose between, which is a guess wearing the
    clothes of an answer. Storage-only, like `mids` itself; no provider ever
    sees it.
    """
    ids = [m for m in (message_ids or []) if m]
    if not ids:
        return False
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        return False
    stored = _stored(row)
    for index in range(len(stored) - 1, -1, -1):
        message = stored[index]
        if message.get("role") != ASSISTANT:
            continue
        merged = list(dict.fromkeys([*(message.get("mids") or []), *ids]))
        # Replaced rather than mutated in place: the column is JSON and
        # SQLAlchemy only notices a reassignment.
        updated = dict(message)
        updated["mids"] = merged
        labelled = {
            mid: label
            for mid, label in (mid_labels or {}).items()
            if isinstance(mid, str) and mid and isinstance(label, str) and label
        }
        if labelled:
            updated["mid_labels"] = {**(message.get("mid_labels") or {}), **labelled}
        stored = list(stored)
        stored[index] = updated
        row.history = stored
        row.updated_at = utcnow()
        session.flush()
        return True
    return False


def clear(session: Session, channel: str, external_id: str) -> None:
    """End the conversation for the bot without destroying the transcript.

    Staff reset (`domain/services/conversation_reset.py`) and the dev harness
    both call this. Both mean "let me start over", never "erase what was
    said" -- so this is a soft delete: the archive keeps every message and the
    next turn starts from nothing. `purge` is the hard one, and no request
    path calls it.
    """
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        return
    stored = _stored(row)
    row.context_start = len(stored)
    row.updated_at = utcnow()
    session.flush()


def purge(session: Session, channel: str, external_id: str) -> int:
    """Actually delete a transcript. Returns how many messages went.

    Deliberately not wired to anything: it exists for a deletion request from
    a real person, run by hand, and it is the only function in this module
    that loses data.
    """
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        return 0
    count = len(_stored(row))
    log.warning(
        "PURGE: deleting %d stored message(s) for %s/%s -- this is not recoverable",
        count,
        channel,
        external_id,
    )
    row.history = []
    row.context_start = 0
    row.updated_at = utcnow()
    session.flush()
    return count
