"""A post-order request that is decided reaches the customer. Every time.

The bot promises «حد هيأكدلك» when it files an add or a swap. For a while
nothing kept that promise: staff pressed reject on a real add request and the
customer was told **nothing at all**. He asked for a hoodie on his order, was
told the team would confirm, and then silence -- which is worse than a
refusal, because a refusal he can act on.

Three outcomes, three messages: approved says what was added and what the
order now comes to, rejected says plainly that it could not be done and what
he can do instead, and a failed apply says we are looking into it *and leaves
the queue item open*. All three follow the rules the rest of the codebase
already has for a message the shop starts: the line is written into `sessions`
inside the transaction that decides it, deliverability is the 24-hour window's
call, and an undeliverable one is recorded `delivered=False` beside a staff
alert rather than pretended.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import runtime as assistant_runtime, session as session_store
from config.settings import settings
from dashboard import queue_api, web as dashboard
from domain.models import Order, QueueKind, QueueStatus, StaffQueueItem, utcnow
from domain.services import auth, carts, identities, notifications, orders, queues
from domain.services.notifications import item_add_requested, item_swap_requested
from integrations.shopify import catalog as shopify_catalog, orders as shopify_orders

SECRET = "test-dashboard-secret"
VARIANT_A = "wanas-hoodie-s-olive"
VARIANT_B = "wanas-hoodie-s-black"
CHANNEL = "whatsapp"
WHO = "201555000444"


@pytest.fixture()
def recording():
    notifications.register_transcript_recorder(assistant_runtime.record_outbound)
    yield
    notifications.register_transcript_recorder(None)


@pytest.fixture()
def client(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(queue_api.router)
    return TestClient(app)


@pytest.fixture()
def logged_in(client, seeded):
    auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


@pytest.fixture()
def order(cairo_rate, seeded, recording) -> Order:
    carts.add(seeded, CHANNEL, WHO, VARIANT_A, 1)
    result = orders.place_order(
        seeded,
        channel=CHANNEL,
        external_id=WHO,
        customer_name="Hazem",
        governorate="Cairo",
        address="1 Test Street",
        contact_phone="01055566677",
    )
    assert "error" not in result, result
    # The customer wrote a moment ago, so the 24-hour window is open.
    identity = identities.get_or_create(seeded, CHANNEL, WHO)
    identity.last_seen_at = utcnow()
    seeded.commit()
    return seeded.get(Order, result["order_id"])


def _add_request(seeded, order) -> str:
    queue_id = item_add_requested(
        seeded,
        order,
        {
            "channel": CHANNEL,
            "external_id": WHO,
            "to_variant_id": VARIANT_B,
            "quantity": 1,
        },
        f"{order.order_id}: add request",
    )
    seeded.commit()
    return queue_id


def _texts(seeded) -> list[str]:
    """Read the transcript and *let go of the database*.

    The dashboard routes under test open their own session. SQLite gives one
    writer at a time, and a read on `seeded` leaves a transaction open that
    the route then waits on forever -- so the read is closed out here rather
    than remembered at each call site.
    """
    try:
        return [m.get("content", "") for m in session_store.transcript(seeded, CHANNEL, WHO)]
    finally:
        seeded.rollback()


def _last_shop_line(seeded) -> dict:
    try:
        lines = [
            m
            for m in session_store.transcript(seeded, CHANNEL, WHO)
            if m.get("role") == "assistant" or m.get("by") == "system"
        ]
    finally:
        seeded.rollback()
    assert lines, "the shop said nothing at all"
    return lines[-1]


# --------------------------------------------------------------------------
# the three outcomes
# --------------------------------------------------------------------------


def test_rejecting_a_request_tells_the_customer(logged_in, seeded, order):
    """The exact production failure: reject was pressed and he heard nothing."""
    queue_id = _add_request(seeded, order)
    before = len(_texts(seeded))

    assert logged_in.post(f"/dashboard/api/queue/{queue_id}/resolve").status_code == 200

    seeded.expire_all()
    texts = _texts(seeded)
    assert len(texts) > before, "the customer was told nothing"
    said = texts[-1]
    assert "مقدرناش" in said, said
    # ...and what he can do instead, which is the half that makes it usable.
    assert "أوردر جديد" in said, said


def test_approving_an_add_tells_the_customer_what_and_how_much(logged_in, seeded, order):
    queue_id = _add_request(seeded, order)

    res = logged_in.post(f"/dashboard/api/queue/{queue_id}/approve-add")
    assert res.status_code == 200, res.text

    seeded.expire_all()
    said = _texts(seeded)[-1]
    # What was added, by its name and not its SKU.
    assert "Hoodie" in said, said
    assert VARIANT_B not in said, "a SKU is not something a customer can read"
    # The new total, because this is cash on delivery and the amount at the
    # door has just changed.
    total = seeded.get(Order, order.order_id).total
    assert str(total) in said or f"{total:.0f}" in said, (said, total)


def test_a_failed_approval_tells_the_customer_and_keeps_the_item_open(
    logged_in, seeded, order, monkeypatch
):
    """The missing-scope case. He must not be left watching a promise expire,
    and the request must still be there to retry once the scope is granted."""
    queue_id = _add_request(seeded, order)

    def denied(*args, **kwargs):
        raise shopify_catalog.ShopifyAccessDenied("denied", "write_order_edits")

    monkeypatch.setattr(shopify_orders, "add_line", denied)
    res = logged_in.post(f"/dashboard/api/queue/{queue_id}/approve-add")
    assert res.status_code == 409
    assert res.json()["error"] == "store_permission"

    seeded.expire_all()
    said = _texts(seeded)[-1]
    assert "لسه معانا" in said, said
    # Nothing technical reaches him.
    assert "store_permission" not in said
    assert "write_order_edits" not in said
    # And the request is still open.
    assert seeded.get(StaffQueueItem, queue_id).status == QueueStatus.OPEN.value


def test_the_holding_line_goes_out_once_however_many_times_it_is_pressed(
    logged_in, seeded, order, monkeypatch
):
    """Staff pressed the failing button three times in production."""
    queue_id = _add_request(seeded, order)

    def denied(*args, **kwargs):
        raise shopify_catalog.ShopifyAccessDenied("denied", "write_order_edits")

    monkeypatch.setattr(shopify_orders, "add_line", denied)
    for _ in range(3):
        assert logged_in.post(f"/dashboard/api/queue/{queue_id}/approve-add").status_code == 409

    seeded.expire_all()
    holding = [t for t in _texts(seeded) if "لسه معانا" in t]
    assert len(holding) == 1, holding


def test_a_swap_resolution_reaches_the_customer_too(logged_in, seeded, order):
    """The same silence existed for swaps -- they are the older of the two."""
    item_swap_requested(
        seeded,
        order,
        {
            "channel": CHANNEL,
            "external_id": WHO,
            "from_variant_id": VARIANT_A,
            "to_variant_id": VARIANT_B,
        },
        f"{order.order_id}: swap request",
    )
    seeded.commit()
    queue_id = queues.open_items(seeded, QueueKind.ITEM_SWAP.value)[0].queue_id
    seeded.rollback()
    before = len(_texts(seeded))

    assert logged_in.post(f"/dashboard/api/queue/{queue_id}/resolve").status_code == 200

    seeded.expire_all()
    texts = _texts(seeded)
    assert len(texts) > before
    assert "التبديل" in texts[-1], texts[-1]


def test_acknowledging_an_alert_says_nothing_to_anybody(logged_in, seeded, order):
    """An alert has no customer behind it. Messaging on one would be the
    opposite mistake -- the shop talking to somebody who asked nothing."""
    alert_id = queues.open_items(seeded, QueueKind.ALERT.value)[0].queue_id
    seeded.rollback()
    before = len(_texts(seeded))
    assert logged_in.post(f"/dashboard/api/queue/{alert_id}/resolve").status_code == 200
    seeded.expire_all()
    assert len(_texts(seeded)) == before


# --------------------------------------------------------------------------
# delivery, not just composition
# --------------------------------------------------------------------------


def test_outside_the_window_the_line_is_recorded_undelivered_and_alerted(
    logged_in, seeded, order
):
    """Meta refuses free-form text more than 24 hours after the customer's
    last message. The transcript must not claim it arrived."""
    queue_id = _add_request(seeded, order)
    identity = identities.get_or_create(seeded, CHANNEL, WHO)
    identity.last_seen_at = utcnow() - timedelta(hours=30)
    seeded.commit()

    assert logged_in.post(f"/dashboard/api/queue/{queue_id}/resolve").status_code == 200

    seeded.expire_all()
    line = _last_shop_line(seeded)
    # `assistant/messages.py` stores the undelivered fact as `delivery`.
    assert line.get("delivery") == "failed", line
    reasons = {i.reason for i in queues.open_items(seeded, QueueKind.ALERT.value)}
    assert "resolution_undelivered" in reasons


def test_the_message_rolls_back_with_the_resolution_it_describes(seeded, order):
    """The line goes in inside the transaction that decides it, so a
    resolution that never commits takes its own message with it."""
    queue_id = _add_request(seeded, order)
    item = seeded.get(StaffQueueItem, queue_id)
    before = len(_texts(seeded))

    notifications.record_request_resolution(
        seeded, item, "rejected", order=order, item_label="Wanas Hoodie Black S"
    )
    seeded.rollback()

    seeded.expire_all()
    assert len(_texts(seeded)) == before


# --------------------------------------------------------------------------
# the structural one
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [QueueKind.ITEM_ADD.value, QueueKind.ITEM_SWAP.value])
def test_a_customer_facing_item_cannot_be_resolved_in_silence(logged_in, seeded, order, kind):
    """The rule, rather than the three cases above one at a time: every route
    that closes a request somebody is waiting on has to leave a line in the
    transcript. A new outcome, or a new route, that forgets fails here."""
    if kind == QueueKind.ITEM_ADD.value:
        queue_id = _add_request(seeded, order)
    else:
        item_swap_requested(
            seeded,
            order,
            {
                "channel": CHANNEL,
                "external_id": WHO,
                "from_variant_id": VARIANT_A,
                "to_variant_id": VARIANT_B,
            },
            "swap",
        )
        seeded.commit()
        queue_id = queues.open_items(seeded, kind)[0].queue_id
        seeded.rollback()

    before = len(_texts(seeded))
    assert logged_in.post(f"/dashboard/api/queue/{queue_id}/resolve").status_code == 200

    seeded.expire_all()
    item = seeded.get(StaffQueueItem, queue_id)
    assert item.status != QueueStatus.OPEN.value, "the item did not actually resolve"
    assert len(_texts(seeded)) > before, (
        f"a {kind} reached {item.status} with nothing said to the customer -- "
        "every resolution of a request somebody is waiting on must reach them"
    )
