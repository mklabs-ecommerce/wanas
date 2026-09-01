"""The order completes, or nothing happened -- never a third state.

The bug these cover, in the shape it was reported: `confirm_order` answered
`tool_failed`, the customer was told the shop had a technical problem, and the
order was sitting in the Shopify admin all along -- created, then cancelled by
the compensating path, or created and left there while the next confirmation
answered `cart_empty` because the first one had already cleared the cart.

So: an order that reached Shopify and the database must be *reported* as
placed whatever else fails afterwards, must survive the rest of the turn, and
a second confirmation must find it instead of a bare refusal.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from domain.db import SessionLocal, session_scope
from domain.models import Counter, Order, ShippingRate, Variant, utcnow
from domain.services import carts, notifications, orders
from domain.services.ids import ORDER_COUNTER

CHANNEL = "whatsapp"
WHO = "201555000222"
VARIANT = "wanas-hoodie-s-olive"


@pytest.fixture()
def priced(seeded, shopify):
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.get(Variant, VARIANT).stock_qty = 5
    seeded.commit()
    shopify.set(VARIANT, qty=5)
    return seeded


def place(session, who: str = WHO, **overrides) -> dict:
    kwargs = {
        "channel": CHANNEL,
        "external_id": who,
        "customer_name": "Nour",
        "governorate": "Cairo",
        "address": "5 Test Street",
        "contact_phone": "01055500022",
    }
    kwargs.update(overrides)
    return orders.place_order(session, **kwargs)


def _live_orders(shopify) -> list[dict]:
    return [o for o in shopify.orders.values() if not o["cancelled"]]


# --------------------------------------------------------------------------
# a placed order is reported as placed
# --------------------------------------------------------------------------


def test_a_broken_notification_does_not_turn_a_placed_order_into_a_failure(
    priced, shopify, monkeypatch
):
    """The alert queue and the confirmation message are bookkeeping about an
    order that already exists on both sides. Letting them raise is what turned
    a completed sale into `tool_failed`."""

    def boom(_session, _order):
        raise RuntimeError("alert queue is having a day")

    monkeypatch.setattr(notifications, "order_confirmed", boom)
    carts.add(priced, CHANNEL, WHO, VARIANT, 1)

    result = place(priced)

    assert "error" not in result, result
    assert result["order_id"] == "WNS-1001"
    assert len(_live_orders(shopify)) == 1, "the Shopify order must not be cancelled"
    with SessionLocal() as fresh:
        assert fresh.get(Order, "WNS-1001") is not None


def test_the_order_is_durable_the_moment_it_is_placed(priced, shopify):
    """Everything after `confirm_order` in a turn -- another tool, the model,
    the session write -- used to be able to roll the order back while the
    Shopify order stayed. It commits at the point Shopify accepted it."""
    carts.add(priced, CHANNEL, WHO, VARIANT, 1)

    result = place(priced)
    priced.rollback()  # the rest of the turn dies

    with SessionLocal() as fresh:
        order = fresh.get(Order, result["order_id"])
        assert order is not None, "the order did not survive the turn"
        assert order.shopify_order_id
        assert carts.is_empty(fresh, CHANNEL, WHO)
    assert len(_live_orders(shopify)) == 1


def test_a_failure_before_the_order_lands_leaves_neither_side_holding_it(
    priced, shopify, monkeypatch
):
    """The other half of the rule: if the local write cannot happen, the
    Shopify order is cancelled and the refusal says so."""

    def boom(_session):
        raise RuntimeError("database went away")

    monkeypatch.setattr(orders, "next_order_id", boom)
    carts.add(priced, CHANNEL, WHO, VARIANT, 1)

    result = place(priced)

    assert result["error"] == "order_failed"
    assert result["stage"] == "local_write"
    assert result["shopify_cancelled"] is True
    assert _live_orders(shopify) == []
    assert shopify.qty(VARIANT) == 5, "the cancel has to restock"
    assert priced.scalars(select(Order)).all() == []
    # And the customer still has their cart, so they can try again.
    assert not carts.is_empty(priced, CHANNEL, WHO)


# --------------------------------------------------------------------------
# confirming twice
# --------------------------------------------------------------------------


def test_confirming_again_finds_the_order_instead_of_answering_cart_empty(priced):
    carts.add(priced, CHANNEL, WHO, VARIANT, 1)
    first = place(priced)

    again = place(priced)

    assert again["error"] == "already_confirmed"
    assert again["order"]["order_id"] == first["order_id"]
    assert again["order"]["reference"] == first["reference"]


def test_confirming_twice_never_places_a_second_order(priced, shopify):
    carts.add(priced, CHANNEL, WHO, VARIANT, 1)
    place(priced)
    place(priced)

    assert len(shopify.orders) == 1
    assert len(priced.scalars(select(Order)).all()) == 1


def test_an_empty_cart_long_after_the_last_order_is_still_cart_empty(priced):
    """The window is what makes `already_confirmed` mean "the one you just
    placed" rather than "any order you ever placed"."""
    carts.add(priced, CHANNEL, WHO, VARIANT, 1)
    order_id = place(priced)["order_id"]

    priced.get(Order, order_id).placed_at = utcnow() - orders.RECENT_ORDER_WINDOW * 2
    priced.commit()

    assert place(priced)["error"] == "cart_empty"


def test_an_empty_cart_with_no_order_at_all_is_still_cart_empty(priced):
    assert place(priced, who="201555000999")["error"] == "cart_empty"


# --------------------------------------------------------------------------
# order ids
# --------------------------------------------------------------------------


def test_a_missing_counter_row_does_not_reissue_an_existing_order_id(priced, shopify):
    """A restored dump, or a `counters` table created next to orders that were
    already there. Restarting at 1001 makes every following order fail on the
    primary key -- after Shopify has already taken the stock."""
    carts.add(priced, CHANNEL, WHO, VARIANT, 1)
    first = place(priced)["order_id"]

    with session_scope() as session:
        session.delete(session.get(Counter, ORDER_COUNTER))

    carts.add(priced, CHANNEL, WHO, VARIANT, 1)
    second = place(priced)

    assert "error" not in second, second
    assert second["order_id"] != first
    assert len(_live_orders(shopify)) == 2


# --------------------------------------------------------------------------
# end to end, through the runtime
# --------------------------------------------------------------------------


def test_a_full_conversation_ends_with_a_live_shopify_order(priced, shopify):
    """Cart to confirmation through the real agent loop, real tools and real
    session storage -- the path a customer actually walks."""
    from assistant.providers.fake import RehearsalProvider
    from assistant.runtime import handle_message

    provider = RehearsalProvider()
    handle_message(CHANNEL, WHO, f"add {VARIANT} 2", db=priced, provider=provider)
    reply = handle_message(
        CHANNEL, WHO, "order Nour | Cairo | 5 Test Street | 01055500022", db=priced, provider=provider
    )

    assert reply.silent and not reply.text, reply.text
    priced.commit()  # the turn's own session write, which the adapter commits
    with SessionLocal() as fresh:
        order = fresh.get(Order, "WNS-1001")
        assert order.status == "Confirmed"
        assert order.total == 1360  # 2 x 650 + 60 shipping
        assert carts.is_empty(fresh, CHANNEL, WHO)

    live = _live_orders(shopify)
    assert len(live) == 1, "the order must not end up cancelled on Shopify"
    assert live[0]["id"] == order.shopify_order_id
    assert shopify.qty(VARIANT) == 3, "Shopify decremented once, not twice"

    sent = [m.text for m in notifications.get_sender(CHANNEL).sent]
    assert any("تم تأكيد طلبك" in text for text in sent), "the customer was never confirmed"
