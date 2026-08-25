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

from sqlalchemy.orm import Session

from assistant.messages import USER
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


def save(session: Session, channel: str, external_id: str, history: list[dict]) -> list[dict]:
    """Store `history` as the live conversation, keeping what came before it.

    `trim` caps what the model is sent; the messages it drops move into the
    archive rather than off the end of the world. Only `SESSION_ARCHIVE_CAP`
    bounds the row, and it says so when it bites.
    """
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
