"""Wipe one conversation back to "never talked to us" -- for staff testing
the bot repeatedly from the same WhatsApp number.

Deliberately narrow: only state keyed by `(channel, external_id)` is
touched. A `Client` row and its real `Order` / `OrderItem` history are never
reachable from here -- the identity is unlinked *from* the client it may
have matched, never the other way around, so testing convenience can never
erase a real sale. See `backend/services/identities.py`'s module docstring
for why the link exists in the first place.
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.models import CartItem, QueueStatus, StaffQueueItem
from backend.services import identities, queues
from chatbot import session as session_store


def reset(session: Session, channel: str, external_id: str, *, staff_id: int) -> None:
    session_store.clear(session, channel, external_id)

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
