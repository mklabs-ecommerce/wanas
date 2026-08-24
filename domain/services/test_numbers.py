"""Staff-marked "this is me testing" phone numbers.

Exists for one reason: `backend/services/dashboard_stats.py` reads live from
Shopify, and a store owner testing the bot from their own WhatsApp number
places real Shopify orders indistinguishable from a customer's -- every
order the bot creates carries the same tags (`shopify_orders.ORDER_TAGS`)
regardless of who placed it. Marking a number here does not change how the
bot treats it in any way; it only tells the statistics page to leave that
number's orders out of revenue, order counts, and best-sellers, the same way
a cancelled order already is.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import TestPhoneNumber, utcnow
from domain.services.identities import phone_variants


def list_numbers(session: Session) -> list[TestPhoneNumber]:
    return list(session.scalars(select(TestPhoneNumber).order_by(TestPhoneNumber.added_at.desc())).all())


def add(session: Session, phone: str, *, note: str | None, staff_id: int) -> TestPhoneNumber:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    row = session.get(TestPhoneNumber, digits)
    if row is None:
        row = TestPhoneNumber(phone=digits)
        session.add(row)
    row.note = note or None
    row.added_at = utcnow()
    row.added_by = staff_id
    session.flush()
    return row


def remove(session: Session, phone: str) -> bool:
    row = session.get(TestPhoneNumber, phone)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def all_variants(session: Session) -> set[str]:
    """Every digit-spelling of every marked number, for matching against
    whatever format Shopify's `customer_phone` happens to be in."""
    out: set[str] = set()
    for row in list_numbers(session):
        out.update(phone_variants(row.phone))
    return out
