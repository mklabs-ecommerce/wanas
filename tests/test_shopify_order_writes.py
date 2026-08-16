"""Selling: what reaches Shopify, and what happens when it cannot.

The read migration left two holes that these close. The bot quoted Shopify's
price and then billed wanas.db's. And it decremented only wanas.db, so the
shelf the storefront sells from never moved and the same shirt could be sold
twice.

The cases that matter are the unhappy ones. A reservation that goes through
followed by a local write that fails is the expensive silent bug here: Shopify
has no savepoint, so nothing puts that stock back except code written on
purpose.
"""

from __future__ import annotations

import pytest

from backend.models import Order, ShippingRate, Variant
from backend.services import carts, orders

VARIANT = "wanas-hoodie-s-olive"
OTHER = "wanas-hoodie-m-black"
WHO = "201555000111"


@pytest.fixture()
def priced(seeded):
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.commit()
    return seeded


def place(session, **overrides):
    args = dict(
        channel="whatsapp",
        external_id=WHO,
        customer_name="Nour",
        governorate="Cairo",
        address="7 Test Street",
        contact_phone="01000000111",
    )
    args.update(overrides)
    return orders.place_order(session, **args)


# --------------------------------------------------------------------------
# the price on the invoice
# --------------------------------------------------------------------------


def test_the_order_is_billed_at_shopifys_price_not_the_local_row(priced, shopify):
    shopify.set(VARIANT, qty=5, price=777)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    result = place(priced)

    assert "order_id" in result, result
    assert result["items"][0]["unit_price"] == 777
    # The catalog row still says what it always said; nothing rewrote it.
    assert priced.get(Variant, VARIANT).price != 777


def test_a_discount_set_on_shopify_reaches_the_order_line(priced, shopify):
    shopify.set(VARIANT, qty=5, price=500, compare=800)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    result = place(priced)

    line = result["items"][0]
    assert line["unit_price"] == 500
    assert line["unit_original_price"] == 800


# --------------------------------------------------------------------------
# the shelf actually moves
# --------------------------------------------------------------------------


def test_selling_takes_the_stock_off_shopify(priced, shopify):
    shopify.set(VARIANT, qty=4)
    carts.add(priced, "whatsapp", WHO, VARIANT, 3)

    assert "order_id" in place(priced)
    assert shopify.qty(VARIANT) == 1


def test_the_local_row_is_kept_in_step(priced, shopify):
    shopify.set(VARIANT, qty=4)
    priced.get(Variant, VARIANT).stock_qty = 4
    priced.commit()
    carts.add(priced, "whatsapp", WHO, VARIANT, 3)

    place(priced)
    assert priced.get(Variant, VARIANT).stock_qty == 1


def test_cancelling_puts_it_back_on_shopify(priced, shopify):
    shopify.set(VARIANT, qty=4)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)
    order_id = place(priced)["order_id"]
    assert shopify.qty(VARIANT) == 2

    order = priced.get(Order, order_id)
    orders.cancel(priced, order)

    assert shopify.qty(VARIANT) == 4


def test_reducing_a_quantity_returns_the_difference(priced, shopify):
    shopify.set(VARIANT, qty=6)
    carts.add(priced, "whatsapp", WHO, VARIANT, 4)
    order_id = place(priced)["order_id"]
    assert shopify.qty(VARIANT) == 2

    orders.modify_quantity(priced, priced.get(Order, order_id), VARIANT, 1)

    assert shopify.qty(VARIANT) == 5


# --------------------------------------------------------------------------
# Shopify says no
# --------------------------------------------------------------------------


def test_sold_out_on_shopify_is_refused_even_when_the_local_row_disagrees(priced, shopify):
    """The exact oversell this work exists to prevent: the storefront took the
    last one, wanas.db has not heard about it."""
    priced.get(Variant, VARIANT).stock_qty = 10
    priced.commit()
    shopify.set(VARIANT, qty=0)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    result = place(priced)

    assert result["error"] == "items_out_of_stock"
    assert result["items"][0]["available"] == 0
    assert priced.query(Order).count() == 0


def test_stock_that_moved_between_the_check_and_the_write_is_not_oversold(
    priced, shopify, monkeypatch
):
    """`compareQuantity` is the guard. Shopify refuses the adjustment rather
    than letting two orders both take the last unit.

    The storefront sells the last one in the gap between our read and our
    write, which is the window no amount of checking beforehand can close.
    """
    from backend.services import shopify_orders

    shopify.set(VARIANT, qty=1)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    real_create = shopify.create_order

    def someone_else_gets_there_first(**kwargs):
        shopify.shelf[VARIANT]["qty"] = 0
        return real_create(**kwargs)

    monkeypatch.setattr(shopify_orders, "create_order", someone_else_gets_there_first)

    result = place(priced)

    assert result["error"] == "items_out_of_stock"
    assert priced.query(Order).count() == 0


# --------------------------------------------------------------------------
# Shopify is not there
# --------------------------------------------------------------------------


def test_an_outage_refuses_rather_than_guessing(priced, shopify):
    """Agreed behaviour: no order is written that Shopify does not know about.
    And the refusal must be distinguishable from "sold out" -- telling a
    customer an in-stock item is gone sends them somewhere else."""
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)
    shopify.down = True

    result = place(priced)

    assert result["error"] == "store_unavailable"
    assert priced.query(Order).count() == 0


def test_a_variant_with_no_sku_is_refused_not_sold_blind(priced, shopify):
    del shopify.shelf[VARIANT]
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    result = place(priced)

    assert result["error"] == "store_unavailable"
    assert priced.query(Order).count() == 0


# --------------------------------------------------------------------------
# compensation
# --------------------------------------------------------------------------


def test_a_failed_local_write_returns_the_reserved_stock(priced, shopify, monkeypatch):
    """Shopify has no savepoint. If the order does not commit, something has to
    put the stock back -- otherwise every crash silently shrinks the shelf."""
    shopify.set(VARIANT, qty=5)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)

    def boom(_session):
        raise RuntimeError("database went away")

    monkeypatch.setattr(orders, "next_order_id", boom)

    with pytest.raises(RuntimeError):
        place(priced)

    assert shopify.qty(VARIANT) == 5


def test_one_bad_line_returns_the_stock_taken_for_the_good_ones(priced, shopify):
    """A two-line order where the second line is sold out. The first line was
    already reserved by then."""
    shopify.set(VARIANT, qty=5)
    shopify.set(OTHER, qty=0)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)
    carts.add(priced, "whatsapp", WHO, OTHER, 1)

    result = place(priced)

    assert result["error"] == "items_out_of_stock"
    assert shopify.qty(VARIANT) == 5, "the good line's stock was never given back"
    assert priced.query(Order).count() == 0


def test_a_rejected_write_that_is_not_a_lost_race_never_reads_as_sold_out(
    priced, shopify, monkeypatch
):
    """A bad `reason`, a location the token cannot write to, a schema change --
    Shopify rejects all of these the same way it rejects a stale compare, and
    an early version of this code called every one of them "stock moved". That
    reaches the customer as "sold out" about a shirt that is on the shelf, and
    sends them somewhere else to buy it.
    """
    from backend.services import shopify_orders

    shopify.set(VARIANT, qty=5)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    def rejected(**kwargs):
        raise shopify_orders.OrderRejected(
            "Shipping line title can't be blank", [{"message": "Shipping line title can't be blank"}]
        )

    monkeypatch.setattr(shopify_orders, "create_order", rejected)

    result = place(priced)

    assert result["error"] == "store_unavailable"
    assert shopify.qty(VARIANT) == 5, "nothing should have left the shelf"
    assert priced.query(Order).count() == 0


def test_a_successful_order_does_not_hand_the_stock_back(priced, shopify):
    """The mirror of the test above, and the one that would fail if the
    compensating release were written to run unconditionally."""
    shopify.set(VARIANT, qty=5)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)

    assert "order_id" in place(priced)
    assert shopify.qty(VARIANT) == 3
    assert shopify.released == []
