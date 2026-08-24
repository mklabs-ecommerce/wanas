"""Who to tell when a sold-out variant comes back.

A customer's failed `add_to_cart` is the only signal this app ever gets that
someone wanted a variant while it was at zero -- there is no separate "notify
me" button. `assistant/tools/cart_tools.py` calls `join` from that exact
moment. `domain/services/reengagement.py` is what later closes the loop.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import StockWaitlistEntry, utcnow


def join(session: Session, variant_id: str, channel: str, external_id: str) -> StockWaitlistEntry:
    """Add this identity to the variant's waitlist, or re-arm it.

    A customer who hit the same sold-out variant a second time after already
    being notified once (and, presumably, not finding it in time) is asking
    again -- `notified_at` resets rather than being left to block a second
    notification forever behind the unique constraint.
    """
    existing = session.scalar(
        select(StockWaitlistEntry).where(
            StockWaitlistEntry.variant_id == variant_id,
            StockWaitlistEntry.channel == channel,
            StockWaitlistEntry.external_id == external_id,
        )
    )
    if existing is None:
        existing = StockWaitlistEntry(
            variant_id=variant_id, channel=channel, external_id=external_id
        )
        session.add(existing)
    elif existing.notified_at is not None:
        existing.notified_at = None
        existing.requested_at = utcnow()
    session.flush()
    return existing


def open_entries(session: Session) -> list[StockWaitlistEntry]:
    return list(
        session.scalars(
            select(StockWaitlistEntry).where(StockWaitlistEntry.notified_at.is_(None))
        ).all()
    )
