"""Cart, keyed by channel identity.

No separate `carts` table -- the identity is the cart key. Nothing here
reserves stock, which is exactly what makes it safe to keep an abandoned cart
indefinitely.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from common.money import money, to_decimal
from config.settings import settings
from domain.models import CartItem, Variant


def _lines(session: Session, channel: str, external_id: str) -> list[CartItem]:
    return list(
        session.scalars(
            select(CartItem)
            .where(CartItem.channel == channel, CartItem.external_id == external_id)
            .order_by(CartItem.id)
        ).all()
    )


def cart_payload(session: Session, channel: str, external_id: str) -> dict:
    lines = []
    subtotal = to_decimal(0)
    item_count = 0
    for row in _lines(session, channel, external_id):
        variant = session.get(Variant, row.variant_id)
        if variant is None:  # a variant retired from the catalog under an open cart
            continue
        line_total = to_decimal(variant.price) * row.quantity
        subtotal += line_total
        item_count += row.quantity
        lines.append(
            {
                "line_id": row.id,
                "variant_id": variant.variant_id,
                "product_name": variant.product.name,
                "size": variant.size,
                "color": variant.color,
                "length": variant.length,
                "quantity": row.quantity,
                "unit_price": money(variant.price),
                "unit_original_price": money(variant.original_price),
                "line_total": money(line_total),
            }
        )
    return {"lines": lines, "item_count": item_count, "subtotal": money(subtotal)}


def get_line(session: Session, channel: str, external_id: str, variant_id: str) -> CartItem | None:
    return session.scalar(
        select(CartItem).where(
            CartItem.channel == channel,
            CartItem.external_id == external_id,
            CartItem.variant_id == variant_id,
        )
    )


def add(session: Session, channel: str, external_id: str, variant_id: str, quantity: int) -> int:
    """Adds to an existing line rather than creating a second one for the same
    variant. Returns the resulting line quantity."""
    line = get_line(session, channel, external_id, variant_id)
    if line is None:
        line = CartItem(channel=channel, external_id=external_id, variant_id=variant_id, quantity=quantity)
        session.add(line)
    else:
        line.quantity = min(line.quantity + quantity, settings.max_quantity_per_line)
    session.flush()
    return line.quantity


def remove_line(session: Session, channel: str, external_id: str, line_id: int) -> None:
    session.execute(
        delete(CartItem).where(
            CartItem.id == line_id,
            CartItem.channel == channel,
            CartItem.external_id == external_id,
        )
    )
    session.flush()


def remove_variant(session: Session, channel: str, external_id: str, variant_id: str) -> None:
    session.execute(
        delete(CartItem).where(
            CartItem.channel == channel,
            CartItem.external_id == external_id,
            CartItem.variant_id == variant_id,
        )
    )
    session.flush()


def clear(session: Session, channel: str, external_id: str) -> None:
    """Called when confirm_order succeeds -- and only then."""
    session.execute(
        delete(CartItem).where(CartItem.channel == channel, CartItem.external_id == external_id)
    )
    session.flush()


def is_empty(session: Session, channel: str, external_id: str) -> bool:
    return not _lines(session, channel, external_id)
