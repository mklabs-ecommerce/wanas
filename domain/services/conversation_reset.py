"""Wipe one conversation back to "never talked to us" -- for staff testing
the bot repeatedly from the same WhatsApp number.

Deliberately narrow: only state keyed by `(channel, external_id)` is
touched. A `Client` row and its real `Order` / `OrderItem` history are never
reachable from here -- the identity is unlinked *from* the client it may
have matched, never the other way around, so testing convenience can never
erase a real sale. See `domain/services/identities.py`'s module docstring
for why the link exists in the first place.

Clearing the chat *history* itself lives in `assistant/session.py`, one
layer up -- domain/ must never import the assistant layer. The assistant
registers its own clearer here at startup -- the same registration-callback
shape `domain/services/notifications.py` already uses for outbound senders
-- so this module can call it without depending on it.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import delete
from sqlalchemy.orm import Session

from domain.models import CartItem, QueueStatus, StaffQueueItem
from domain.services import (
    identities,
    queues,
)

HistoryClearer = Callable[[Session, str, str], None]

_history_clearer: HistoryClearer | None = None


def register_history_clearer(clearer: HistoryClearer) -> None:
    """Called once at startup by the assistant layer with its own
    `session.clear`. Until it is, `reset()` still clears everything else --
    only the chat history line is skipped, which only matters to tests that
    never register one."""
    global _history_clearer
    _history_clearer = clearer


def reset(session: Session, channel: str, external_id: str, *, staff_id: int) -> None:
    if _history_clearer is not None:
        _history_clearer(session, channel, external_id)

    identity = identities.get(session, channel, external_id)
    if identity is not None:
        # Unlinked, not deleted -- the `Client` row this may have pointed at
        # (and its real orders) is a different table this never touches.
        identity.client_id = None
        identity.pending_link = None
        identity.paused_until_staff_reply = False
        session.flush()

    session.execute(delete(CartItem).where(CartItem.channel == channel, CartItem.external_id == external_id))

    open_items = (
        session.query(StaffQueueItem)
        .filter_by(channel=channel, external_id=external_id, status=QueueStatus.OPEN.value)
        .all()
    )
    for item in open_items:
        queues.resolve(session, item.queue_id, staff_id)
