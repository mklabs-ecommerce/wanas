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

from domain.models import Order, ShippingRate, Variant
from domain.services import (
    carts,
    orders,
)

VARIANT = "wanas-hoodie-s-olive"
OTHER = "wanas-hoodie-m-black"
WHO = "201555000111"


@pytest.fixture()
def priced(seeded):
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.commit()
    return seeded


def place(session, **overrides):
    args = {
        "channel": "whatsapp",
        "external_id": WHO,
        "customer_name": "Nour",
        "governorate": "Cairo",
        "address": "7 Test Street",
        "contact_phone": "01000000111",
    }
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
    """The compare-and-swap is the guard. Shopify refuses the adjustment rather
    than letting two orders both take the last unit.

    The storefront sells the last one in the gap between our read and our
    write, which is the window no amount of checking beforehand can close.
    """
    from integrations.shopify import orders as shopify_orders

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

    assert place(priced)["error"] == "order_failed"

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
    from integrations.shopify import orders as shopify_orders

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


# --------------------------------------------------------------------------
# the compare-and-swap itself
# --------------------------------------------------------------------------
#
# Everything above reserves through the fake shelf, which honours the compare
# without caring what the field is called. Shopify does care: it renamed
# `compareQuantity` to `changeFromQuantity` in 2026-01, and versions before
# that have no compare field at all -- a reserve there would be a
# last-write-wins set, which is the oversell this module exists to prevent.


class _Recorder:
    def __init__(self, version, errors=()):
        self.version = version
        self.calls = []
        self.errors = list(errors)

    def __call__(self, query, variables=None):
        self.calls.append(variables)
        return {"inventorySetQuantities": {"userErrors": self.errors}}


@pytest.mark.no_shopify
def test_a_reservation_names_the_quantity_it_expects_to_find(monkeypatch):
    from integrations.shopify import inventory as shopify_inventory

    rec = _Recorder("2026-07")
    monkeypatch.setattr(shopify_inventory, "get_client", lambda: rec)
    monkeypatch.setattr(shopify_inventory, "location_id", lambda: "gid://shopify/Location/1")

    shopify_inventory.reserve(
        [{"inventory_item_id": "gid://shopify/InventoryItem/9", "quantity": 2, "expected": 5}],
        "W-1",
    )

    line = rec.calls[0]["input"]["quantities"][0]
    assert line["quantity"] == 3
    assert line["changeFromQuantity"] == 5, "the shelf we believe we read"
    assert "compareQuantity" not in line
    assert "ignoreCompareQuantity" not in rec.calls[0]["input"]
    assert rec.calls[0]["key"], "Shopify refuses the mutation without one"


@pytest.mark.no_shopify
def test_an_api_version_without_the_compare_refuses_rather_than_oversells(monkeypatch):
    """Better to tell the customer the store is unavailable than to sell the
    last shirt twice because the guard quietly was not there."""
    from integrations.shopify import inventory as shopify_inventory
    from integrations.shopify.client import ShopifyUnavailable

    rec = _Recorder("2025-01")
    monkeypatch.setattr(shopify_inventory, "get_client", lambda: rec)
    monkeypatch.setattr(shopify_inventory, "location_id", lambda: "gid://shopify/Location/1")

    with pytest.raises(ShopifyUnavailable, match="SHOPIFY_API_VERSION"):
        shopify_inventory.reserve(
            [{"inventory_item_id": "gid://shopify/InventoryItem/9", "quantity": 1, "expected": 5}],
            "W-1",
        )

    assert rec.calls == [], "nothing may reach Shopify unguarded"


@pytest.mark.no_shopify
def test_shopifys_own_words_for_a_lost_race_are_read_as_one(monkeypatch):
    """Verbatim from a live refusal. Read as anything else, the customer is
    told the store is down when in truth somebody just bought the last one --
    and the honest answer, a re-read of the shelf, never happens."""
    from integrations.shopify import inventory as shopify_inventory

    rec = _Recorder(
        "2026-07",
        errors=[
            {
                "field": None,
                "code": None,
                "message": (
                    "The changeFromQuantity argument no longer matches "
                    "the persisted quantity."
                ),
            }
        ],
    )
    monkeypatch.setattr(shopify_inventory, "get_client", lambda: rec)
    monkeypatch.setattr(shopify_inventory, "location_id", lambda: "gid://shopify/Location/1")

    with pytest.raises(shopify_inventory.StockMoved):
        shopify_inventory.reserve(
            [{"inventory_item_id": "gid://shopify/InventoryItem/9", "quantity": 1, "expected": 5}],
            "W-1",
        )


@pytest.mark.no_shopify
def test_two_reservations_of_the_same_thing_are_two_writes(monkeypatch):
    """The idempotency key Shopify demands is per attempt, not per order. A
    key derived from the order and the numbers would make an item removed and
    added back come off the shelf once and be sold twice."""
    from integrations.shopify import inventory as shopify_inventory

    rec = _Recorder("2026-07")
    monkeypatch.setattr(shopify_inventory, "get_client", lambda: rec)
    monkeypatch.setattr(shopify_inventory, "location_id", lambda: "gid://shopify/Location/1")

    change = [{"inventory_item_id": "gid://shopify/InventoryItem/9", "quantity": 1, "expected": 5}]
    shopify_inventory.reserve(change, "W-1")
    shopify_inventory.reserve(change, "W-1")

    assert rec.calls[0]["key"] != rec.calls[1]["key"]
