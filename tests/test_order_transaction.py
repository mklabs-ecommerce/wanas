"""The atomic order transaction -- the expensive-if-silent bug.

Two concurrent orders for the last unit: exactly one succeeds. A failure
mid-transaction leaves no stock decremented and no order written.

These run against whatever DATABASE_URL points at, so the same assertions can
be re-run on PostgreSQL without editing anything here.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from backend.db import SessionLocal, session_scope
from backend.models import Order, OrderItem, ShippingRate, Variant
from backend.services import carts, orders

VARIANT = "wanas-hoodie-s-olive"


@pytest.fixture()
def last_unit(cairo_rate, shopify):
    session = cairo_rate  # fixture returns the rate; grab a session off it
    from backend.db import SessionLocal as _SL

    with _SL() as s:
        variant = s.get(Variant, VARIANT)
        variant.stock_qty = 1
        s.commit()
    # Shopify is the shelf the order path checks. Setting only the local row
    # would leave both racing orders looking at a well-stocked store.
    shopify.set(VARIANT, qty=1)
    return VARIANT


def _stock(variant_id: str = VARIANT) -> int:
    with SessionLocal() as session:
        return session.get(Variant, variant_id).stock_qty


def _place(external_id: str, results: list, index: int, barrier: threading.Barrier | None = None) -> None:
    try:
        if barrier is not None:
            # Both threads reach their transaction at the same moment, so the
            # decrement really is contended rather than incidentally ordered.
            barrier.wait(timeout=10)
        with session_scope() as session:
            carts.add(session, "whatsapp", external_id, VARIANT, 1)
            results[index] = orders.place_order(
                session,
                channel="whatsapp",
                external_id=external_id,
                customer_name=f"Customer {index}",
                governorate="Cairo",
                address="1 Test Street",
                contact_phone=f"0100000000{index}",
            )
    except Exception as exc:  # surfaced by the assertions below
        results[index] = {"error": "exception", "detail": repr(exc)}


def test_two_concurrent_orders_for_the_last_unit(last_unit):
    """The database decides, not application code: one order, one refusal."""
    results: list = [None, None]
    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=_place, args=("2010000000A", results, 0, barrier)),
        threading.Thread(target=_place, args=("2010000000B", results, 1, barrier)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    succeeded = [r for r in results if r and "order_id" in r]
    refused = [r for r in results if r and r.get("error") == "items_out_of_stock"]

    assert len(succeeded) == 1, results
    assert len(refused) == 1, results
    assert _stock() == 0

    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1


def test_refusal_writes_nothing(cairo_rate, shopify):
    """The loser of the race must leave the catalog and the order book
    untouched, including any line that did decrement before the failure --
    on Shopify's shelf as well as the local one, since Shopify has no
    savepoint to roll back for us."""
    with session_scope() as session:
        session.get(Variant, VARIANT).stock_qty = 5
        session.get(Variant, "wanas-hoodie-m-black").stock_qty = 0
    shopify.set(VARIANT, qty=5)
    shopify.set("wanas-hoodie-m-black", qty=0)

    with session_scope() as session:
        carts.add(session, "whatsapp", "201999", VARIANT, 2)
        carts.add(session, "whatsapp", "201999", "wanas-hoodie-m-black", 1)
        result = orders.place_order(
            session,
            channel="whatsapp",
            external_id="201999",
            customer_name="Mona",
            governorate="Cairo",
            address="2 Test Street",
            contact_phone="01099999999",
        )

    assert result["error"] == "items_out_of_stock"
    assert [i["variant_id"] for i in result["items"]] == ["wanas-hoodie-m-black"]
    # The successful decrement on the first line was rolled back with it.
    assert _stock() == 5
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        # And the cart still holds what the customer chose.
        assert len(carts._lines(session, "whatsapp", "201999")) == 2


def test_crash_mid_transaction_rolls_back_stock_and_order(cairo_rate, monkeypatch):
    """A crash after the decrement but before the order commits."""
    before = _stock()

    def boom(_session):
        raise RuntimeError("database went away")

    monkeypatch.setattr(orders, "next_order_id", boom)

    with pytest.raises(RuntimeError):
        with session_scope() as session:
            carts.add(session, "whatsapp", "201888", VARIANT, 1)
            orders.place_order(
                session,
                channel="whatsapp",
                external_id="201888",
                customer_name="Hana",
                governorate="Cairo",
                address="3 Test Street",
                contact_phone="01088888888",
            )

    assert _stock() == before
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(OrderItem)) == 0


def test_successful_order_decrements_and_clears_the_cart(cairo_rate):
    before = _stock()
    with session_scope() as session:
        carts.add(session, "whatsapp", "201777", VARIANT, 2)
        result = orders.place_order(
            session,
            channel="whatsapp",
            external_id="201777",
            customer_name="Salma",
            governorate="Cairo",
            address="4 Test Street",
            contact_phone="01077777777",
        )

    assert result["order_id"] == "WNS-1001"
    assert result["status"] == "Confirmed"
    assert result["payment_method"] == "cash_on_delivery"
    assert result["subtotal"] == 1300  # 2 x 650
    assert result["discount_amount"] == 0
    assert result["shipping_fee"] == 60
    assert result["total"] == 1360
    assert _stock() == before - 2

    with SessionLocal() as session:
        assert carts.is_empty(session, "whatsapp", "201777")
        order = session.get(Order, "WNS-1001")
        assert order.governorate == "Cairo"
        assert order.payment_status == "pending"
        item = order.items[0]
        # Snapshots, not joins: the packing slip has to survive a rename.
        assert (item.product_name, item.size, item.color, item.length) == (
            "WANAS Hoodie",
            "S",
            "Olive",
            None,
        )


def test_order_ids_are_sequential_from_1001(cairo_rate):
    ids = []
    for n in range(3):
        with session_scope() as session:
            carts.add(session, "whatsapp", f"20166{n}", VARIANT, 1)
            ids.append(
                orders.place_order(
                    session,
                    channel="whatsapp",
                    external_id=f"20166{n}",
                    customer_name="Ali",
                    governorate="Cairo",
                    address="5 Test Street",
                    contact_phone=f"0106660000{n}",
                )["order_id"]
            )
    assert ids == ["WNS-1001", "WNS-1002", "WNS-1003"]


def test_shipping_fee_is_copied_not_looked_up(cairo_rate):
    with session_scope() as session:
        carts.add(session, "whatsapp", "201555", VARIANT, 1)
        result = orders.place_order(
            session,
            channel="whatsapp",
            external_id="201555",
            customer_name="Nour",
            governorate="Cairo",
            address="6 Test Street",
            contact_phone="01055555555",
        )
    assert result["shipping_fee"] == 60

    with session_scope() as session:
        session.get(ShippingRate, "Cairo").fee = 999

    with SessionLocal() as session:
        order = session.get(Order, result["order_id"])
        assert float(order.shipping_fee) == 60
        assert float(order.total) == float(order.subtotal) + 60
