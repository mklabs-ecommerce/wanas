"""Inventory, catalog, shipping, notifications, and the after-the-order paths."""

from __future__ import annotations

from sqlalchemy import select

from domain.db import SessionLocal, session_scope
from domain.models import Client, Order, QueueKind, ShippingRate, StaffQueueItem, Variant
from domain.services import (
    carts,
    catalog,
    identities,
    inventory,
    notifications,
    orders,
    queues,
    shipping,
)

VARIANT = "wanas-hoodie-s-olive"


# --- Inventory ------------------------------------------------------------


def test_decrement_is_conditional(seeded, shopify):
    # Shopify is the shelf the decrement checks, so it is the one that has to
    # say "two left". The local row is kept in step for the assertions below.
    shopify.set(VARIANT, qty=2)
    seeded.get(Variant, VARIANT).stock_qty = 2
    seeded.flush()

    assert inventory.decrement(seeded, VARIANT, 2).ok is True
    assert seeded.get(Variant, VARIANT).stock_qty == 0

    result = inventory.decrement(seeded, VARIANT, 1)
    assert result.ok is False
    assert result.available == 0
    # A refused decrement must not have moved the number.
    assert seeded.get(Variant, VARIANT).stock_qty == 0


def test_release_adds_back(seeded):
    seeded.get(Variant, VARIANT).stock_qty = 4
    seeded.flush()
    inventory.release(seeded, VARIANT, 3)
    assert seeded.get(Variant, VARIANT).stock_qty == 7


def test_record_sold_floors_at_zero(seeded):
    """`record_sold` is unconditional bookkeeping after Shopify has already
    sold the item -- it must never write a negative local stock number even
    if the local row disagreed with Shopify going in."""
    seeded.get(Variant, VARIANT).stock_qty = 1
    seeded.flush()
    inventory.record_sold(seeded, VARIANT, 5)
    assert seeded.get(Variant, VARIANT).stock_qty == 0


def test_local_decrement_sql_is_portable_to_postgres():
    """`_local_decrement` used to write `func.max(stock_qty - n, 0)`. SQLite
    overloads `max()` to also work as a 2-argument scalar function, so every
    test here passed -- but PostgreSQL's `max()` is aggregate-only, and
    `max(a, b)` raised `UndefinedFunction` on *every* order placed in
    production. This captures the actual statement `_local_decrement` builds
    and compiles it against the postgres dialect (no live connection needed)
    so this class of SQLite-only-passing bug cannot silently return -- see
    AGENTS.md / CLAUDE.md on why the suite must be run against Postgres
    before deploying.
    """
    from sqlalchemy.dialects import postgresql

    from domain.services.inventory import _local_decrement

    captured = {}

    class FakeSession:
        def execute(self, stmt):
            captured["stmt"] = stmt

        def expire_all(self):
            pass

    _local_decrement(FakeSession(), VARIANT, 1)
    compiled = str(captured["stmt"].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "CASE WHEN" in compiled
    assert "max(" not in compiled.lower()


def test_threshold_breach(seeded):
    variant = seeded.get(Variant, VARIANT)
    variant.stock_qty = 3
    variant.low_stock_threshold = 2
    seeded.flush()
    assert inventory.breached_threshold(seeded, VARIANT) is False
    inventory.decrement(seeded, VARIANT, 1)
    assert inventory.breached_threshold(seeded, VARIANT) is True


# --- Catalog --------------------------------------------------------------


def test_categories_lead_collections_trail(seeded):
    payload = catalog.get_categories(seeded)
    assert [c["category"] for c in payload["categories"]][0] == "T-Shirts"
    assert sum(c["product_count"] for c in payload["categories"]) == 18
    assert payload["departments"] == ["unisex", "women"]
    assert payload["collections"] == ["CAIROKEE MERCH", "WINTER COLLECTION"]
    assert "oversized" in payload["styles"]


def test_price_from_and_to_are_computed_from_variants(seeded):
    result = catalog.get_products(seeded, query="WANAS Hoodie")
    hoodie = next(p for p in result["products"] if p["product_id"] == "wanas-hoodie")
    # Priced per colour: 650 in black and olive, 700 in grey.
    assert hoodie["price_from"] == 650
    assert hoodie["price_to"] == 700
    # The strike-through number, not the top of a range.
    assert hoodie["original_price_to"] == 900
    assert hoodie["on_sale"] is True


def test_search_matches_colour_not_just_name(seeded):
    """"الهودي الزيتي" has to resolve off a colour that is no longer in any
    product name."""
    result = catalog.get_products(seeded, query="olive hoodie")
    ids = [p["product_id"] for p in result["products"]]
    assert "wanas-hoodie" in ids


def test_style_and_department_filters(seeded):
    women = catalog.get_products(seeded, department="women")
    assert women["count"] == 2
    oversized = catalog.get_products(seeded, style="oversized")
    assert oversized["count"] >= 1
    assert all("oversized" in p["style"] for p in oversized["products"])


def test_get_variants_returns_sold_out_too(seeded):
    payload = catalog.get_variants(seeded, "wanas-hoodie")
    assert len(payload["variants"]) > len(payload["in_stock"])
    assert any(v["status"] == "sold_out" for v in payload["variants"])
    assert payload["has_size_chart"] is True
    assert payload["images"]


def test_alternatives_prefer_same_colour_then_same_size(seeded, shopify):
    for v in seeded.scalars(select(Variant).where(Variant.product_id == "wanas-hoodie")).all():
        v.stock_qty = 0
        shopify.set(v.variant_id, qty=0)
    for vid in ("wanas-hoodie-l-olive", "wanas-hoodie-m-black"):
        seeded.get(Variant, vid).stock_qty = 5
        shopify.set(vid, qty=5)
    seeded.flush()

    target = seeded.get(Variant, "wanas-hoodie-m-olive")
    alts = catalog.alternatives_for(seeded, target)
    assert alts[0]["variant_id"] == "wanas-hoodie-l-olive"  # same colour first
    assert alts[1]["variant_id"] == "wanas-hoodie-m-black"  # then same size


# --- Shipping -------------------------------------------------------------


def test_governorate_resolution(seeded):
    assert shipping.resolve(seeded, "Cairo") == "Cairo"
    assert shipping.resolve(seeded, "القاهرة") == "Cairo"
    assert shipping.resolve(seeded, "القاهره") == "Cairo"
    assert shipping.resolve(seeded, "مصر الجديدة") == "Cairo"
    assert shipping.resolve(seeded, "الجيزه") == "Giza"
    assert shipping.resolve(seeded, "6 اكتوبر") == "Giza"
    assert shipping.resolve(seeded, "alex") == "Alexandria"
    assert shipping.resolve(seeded, "المنصورة") == "Dakahlia"
    assert shipping.resolve(seeded, "Atlantis") is None


def test_fee_is_none_until_the_shop_sets_it(seeded):
    assert shipping.get_fee(seeded, "Cairo") is None
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.flush()
    assert float(shipping.get_fee(seeded, "Cairo")) == 60


def test_order_refused_for_an_unpriced_governorate(seeded):
    carts.add(seeded, "whatsapp", "201222", VARIANT, 1)
    result = orders.place_order(
        seeded,
        channel="whatsapp",
        external_id="201222",
        customer_name="Yara",
        governorate="Aswan",
        address="7 Test Street",
        contact_phone="01022222222",
    )
    assert result == {"error": "no_rate_set", "governorate": "Aswan", "known_governorate": True}


# --- Clients and identities ----------------------------------------------


def test_identity_starts_with_no_client(seeded):
    identity = identities.get_or_create(seeded, "whatsapp", "201333")
    assert identity.client_id is None
    assert identities.client_for(seeded, "whatsapp", "201333") is None


def test_checkout_never_links_silently(cairo_rate):
    """An exact phone match creates a fresh record and records the match for
    the bot to ask about -- it does not reuse the existing client."""
    with session_scope() as session:
        existing = Client(full_name="Mona Adel", phone="01011112222", address="Old address", status="active")
        session.add(existing)
        session.flush()
        existing_id = existing.client_id

    with session_scope() as session:
        carts.add(session, "whatsapp", "201444", VARIANT, 1)
        orders.place_order(
            session,
            channel="whatsapp",
            external_id="201444",
            customer_name="M Adel",
            governorate="Cairo",
            address="New address",
            contact_phone="01011112222",
        )

    with SessionLocal() as session:
        identity = identities.get(session, "whatsapp", "201444")
        assert identity.client_id != existing_id
        assert identity.pending_link["matched_on"] == "phone"
        assert identity.pending_link["client_id"] == f"c_{existing_id}"
        # Masked: an unconfirmed match is, by definition, possibly a stranger.
        assert identity.pending_link["masked_name"] == "M… A…"
        assert session.get(Client, existing_id).address == "Old address"


def test_blocked_client_cannot_order(cairo_rate):
    with session_scope() as session:
        blocked = Client(full_name="Blocked", phone="01033334444", address="x", status="blocked")
        session.add(blocked)
        session.flush()
        identity = identities.get_or_create(session, "whatsapp", "201555")
        identity.client_id = blocked.client_id

    with session_scope() as session:
        carts.add(session, "whatsapp", "201555", VARIANT, 1)
        result = orders.place_order(
            session,
            channel="whatsapp",
            external_id="201555",
            customer_name="Blocked",
            governorate="Cairo",
            address="x",
            contact_phone="01033334444",
        )
    assert result == {"error": "client_blocked"}


# --- Notifications --------------------------------------------------------


def _place(session, external_id="201666", qty=1):
    carts.add(session, "whatsapp", external_id, VARIANT, qty)
    return orders.place_order(
        session,
        channel="whatsapp",
        external_id=external_id,
        customer_name="Layla",
        governorate="Cairo",
        address="8 Test Street",
        contact_phone="01066667777",
    )


def test_order_confirmed_alerts_staff_and_messages_the_customer(cairo_rate):
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        with session_scope() as session:
            result = _place(session)
    finally:
        notifications.register_sender(notifications.LogSender())

    assert "order_id" in result
    with SessionLocal() as session:
        alerts = queues.open_items(session, QueueKind.ALERT.value)
        assert any(a.reason == "order_confirmed" for a in alerts)

    assert len(sender.sent) == 1
    body = sender.sent[0].text
    # The number the customer is given is Shopify's, not the internal one.
    assert result["reference"] in body
    assert result["order_id"] not in body
    # Each item line carries its variant: colour and size, always.
    assert "WANAS Hoodie — Olive, S" in body
    assert "710" in body  # 650 + 60 shipping


def test_low_stock_breach_alerts(cairo_rate):
    with session_scope() as session:
        variant = session.get(Variant, VARIANT)
        variant.stock_qty = 3
        variant.low_stock_threshold = 2

    with session_scope() as session:
        _place(session, qty=1)

    with SessionLocal() as session:
        alerts = queues.open_items(session, QueueKind.ALERT.value)
        assert any(a.reason == "low_stock" for a in alerts)


def test_confirmation_is_not_sent_when_the_order_never_lands(cairo_rate, monkeypatch):
    """A customer must never be told about an order the database does not
    have. The order commits at the moment Shopify accepts it, so the case that
    can still produce that mismatch is the local write failing -- and then
    nothing is written, nothing is sent, and the Shopify order is cancelled."""
    sender = notifications.LogSender()
    notifications.register_sender(sender)

    def boom(_session):
        raise RuntimeError("database went away")

    monkeypatch.setattr(orders, "next_order_id", boom)

    try:
        with session_scope() as session:
            result = _place(session)
    finally:
        notifications.register_sender(notifications.LogSender())

    assert result["error"] == "order_failed"
    assert sender.sent == []
    with SessionLocal() as session:
        assert session.scalar(select(Order)) is None


def test_a_crash_after_the_order_cannot_unplace_it(cairo_rate):
    """The other side of the same rule. Once Shopify has the sale, the local
    order is committed with it: a turn that dies afterwards must not leave a
    Shopify order nothing on our side knows about."""
    sender = notifications.LogSender()
    notifications.register_sender(sender)

    try:
        try:
            with session_scope() as session:
                result = _place(session)
                raise RuntimeError("crash after place_order")
        except RuntimeError:
            pass
    finally:
        notifications.register_sender(notifications.LogSender())

    with SessionLocal() as session:
        order = session.get(Order, result["order_id"])
        assert order is not None
        assert order.shopify_order_id
    assert len(sender.sent) == 1, "the customer is told, because the order is real"


# --- After the order ------------------------------------------------------


def test_modify_quantity_recomputes_total_and_keeps_shipping(cairo_rate):
    with session_scope() as session:
        result = _place(session, qty=1)
        order_id = result["order_id"]

    with session_scope() as session:
        order = session.get(Order, order_id)
        updated = orders.modify_quantity(session, order, VARIANT, 3)

    assert updated["subtotal"] == 1950
    assert updated["shipping_fee"] == 60  # never re-quoted
    assert updated["total"] == 2010
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        assert order.modification_log[-1]["from"] == 1
        assert order.modification_log[-1]["to"] == 3


def test_modify_quantity_refuses_after_shipped(cairo_rate):
    with session_scope() as session:
        order_id = _place(session)["order_id"]
    with session_scope() as session:
        order = session.get(Order, order_id)
        orders.advance_status(session, order, "Packed")
        orders.advance_status(session, order, "Shipped")
    with session_scope() as session:
        order = session.get(Order, order_id)
        assert orders.modify_quantity(session, order, VARIANT, 2) == {
            "error": "not_modifiable",
            "status": "Shipped",
        }
        assert orders.cancel(session, order) == {"error": "not_modifiable", "status": "Shipped"}


def test_cancel_returns_stock(cairo_rate):
    with session_scope() as session:
        before = session.get(Variant, VARIANT).stock_qty
        order_id = _place(session, qty=2)["order_id"]
        assert session.get(Variant, VARIANT).stock_qty == before - 2

    with session_scope() as session:
        order = session.get(Order, order_id)
        assert orders.cancel(session, order)["status"] == "Cancelled"

    with SessionLocal() as session:
        assert session.get(Variant, VARIANT).stock_qty == before
        assert any(
            a.reason == "order_cancelled" for a in queues.open_items(session, QueueKind.ALERT.value)
        )


def test_status_pushes_and_feedback_request(cairo_rate):
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        with session_scope() as session:
            order_id = _place(session)["order_id"]
        sender.clear()
        for status in ("Packed", "Shipped", "Delivered"):
            with session_scope() as session:
                orders.advance_status(session, session.get(Order, order_id), status)
    finally:
        notifications.register_sender(notifications.LogSender())

    templates = [m.template for m in sender.sent]
    assert templates == ["status_packed", "status_shipped", "status_delivered", "feedback_request"]


def test_status_transitions_are_forward_only(cairo_rate):
    with session_scope() as session:
        order_id = _place(session)["order_id"]
    with session_scope() as session:
        order = session.get(Order, order_id)
        assert orders.advance_status(session, order, "Shipped")["error"] == "bad_transition"
        assert orders.advance_status(session, order, "Packed")["status"] == "Packed"
        assert orders.advance_status(session, order, "Confirmed")["error"] == "bad_transition"


def test_feedback_only_after_delivery_and_only_once(cairo_rate):
    with session_scope() as session:
        order_id = _place(session)["order_id"]

    with session_scope() as session:
        order = session.get(Order, order_id)
        assert orders.submit_feedback(session, order, 5, None)["error"] == "not_delivered"

    with session_scope() as session:
        order = session.get(Order, order_id)
        for status in ("Packed", "Shipped", "Delivered"):
            orders.advance_status(session, order, status)

    with session_scope() as session:
        order = session.get(Order, order_id)
        assert orders.submit_feedback(session, order, 5, "حلو أوي")["saved"] is True

    with session_scope() as session:
        order = session.get(Order, order_id)
        assert orders.submit_feedback(session, order, 4, None)["error"] == "already_rated"


def test_queue_resolution_is_first_action_wins(seeded):
    item = queues.enqueue(seeded, kind=QueueKind.HANDOFF.value, summary="unclear", reason="unclear")
    seeded.flush()
    assert queues.resolve(seeded, item.queue_id, staff_id=None) is not None
    assert queues.resolve(seeded, item.queue_id, staff_id=None) is None
    assert seeded.get(StaffQueueItem, item.queue_id).status == "resolved"
