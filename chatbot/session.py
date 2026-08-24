"""Conversation history, in the database, keyed by channel + external_id.

Not in process memory: the server has to restart without customers losing
their conversation, and more than one instance has to be able to run behind a
load balancer.

Losing a session loses nothing real -- the cart is stored separately and
survives.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import UNREADABLE_HISTORY, SessionRow, utcnow
from chatbot.messages import USER

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
    updated = row.updated_at
    now = utcnow()
    if updated.tzinfo is None:  # SQLite hands back naive datetimes
        updated = updated.replace(tzinfo=now.tzinfo)
    return now - updated > timedelta(hours=settings.session_expiry_hours)


def load(session: Session, channel: str, external_id: str) -> list[dict]:
    """History for this identity, or a fresh one after 6 hours of silence.

    A row written outside the app -- a manual edit or a restore -- can hold
    text that is not JSON at all. The decode happens while the row is loaded,
    inside `session.get`, and is guarded there (`LenientJSON`): a poisoned
    column arrives here as `UNREADABLE_HISTORY` instead of raising on every
    turn. Answer with an empty history and leave the stored value untouched:
    it stays exactly as it is until the next successful save overwrites it,
    so nothing here silently deletes data.
    """
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        return []
    if _expired(row):
        row.history = []
        row.updated_at = utcnow()
        session.flush()
        return []
    history = row.history
    if history is UNREADABLE_HISTORY or not isinstance(history, list):
        log.error(
            "session %s/%s has unreadable history (%s); this turn starts empty "
            "(stored value left in place until the next save)",
            channel,
            external_id,
            type(history).__name__,
        )
        return []
    return list(history)


def save(session: Session, channel: str, external_id: str, history: list[dict]) -> list[dict]:
    trimmed = trim(history)
    row = session.get(SessionRow, (channel, external_id))
    if row is None:
        row = SessionRow(channel=channel, external_id=external_id, history=trimmed)
        session.add(row)
    else:
        row.history = trimmed
    row.updated_at = utcnow()
    session.flush()
    return trimmed


def append(session: Session, channel: str, external_id: str, *messages: dict) -> list[dict]:
    history = load(session, channel, external_id) + list(messages)
    return save(session, channel, external_id, history)


def clear(session: Session, channel: str, external_id: str) -> None:
    row = session.get(SessionRow, (channel, external_id))
    if row is not None:
        row.history = []
        row.updated_at = utcnow()
        session.flush()
