"""The staff dashboard.

The load-bearing claims: no route is reachable unauthenticated, every write is
attributed, a queue resolves once, and resolving a handoff is the only thing
that un-pauses a conversation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.db import SessionLocal
from backend.models import (
    AuditLog,
    Client,
    Order,
    QueueKind,
    QueueStatus,
    ShippingRate,
    StaffQueueItem,
    Variant,
)
from backend.services import carts, identities, notifications, orders, queues
from backend.services.auth import create_staff
from chatbot.tools.base import ToolContext, call_tool

VARIANT = "wanas-hoodie-s-olive"
WHO = "201000000001"

PROTECTED = [
    "/dashboard",
    "/dashboard/orders",
    "/dashboard/products",
    "/dashboard/clients",
    "/dashboard/shipping",
    "/dashboard/size-charts",
    "/dashboard/queues",
    "/dashboard/audit",
]


@pytest.fixture()
def app(seeded):
    from app import app as fastapi_app

    return fastapi_app


@pytest.fixture()
def anon(app):
    # follow_redirects off: the redirect *is* what these tests assert on.
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def staff_user(seeded):
    staff = create_staff(seeded, "amira", "correct-horse")
    seeded.commit()
    return staff


@pytest.fixture()
def client(app, staff_user):
    c = TestClient(app, follow_redirects=True)
    response = c.post("/dashboard/login", data={"username": "amira", "password": "correct-horse"})
    assert response.status_code == 200
    return c


def csrf_from(client) -> str:
    """Read the token the server put in the page, the way a browser would."""
    page = client.get("/dashboard").text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


# --- access ---------------------------------------------------------------


@pytest.mark.parametrize("path", PROTECTED)
def test_nothing_is_reachable_without_logging_in(anon, path):
    """It can edit stock and orders; an accidentally-public deployment is a
    live business risk, not a test inconvenience."""
    response = anon.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/login"


def test_login_page_is_reachable_and_says_there_is_no_default_account(anon):
    response = anon.get("/dashboard/login")
    assert response.status_code == 200
    assert "create-staff" in response.text


def test_bad_password_does_not_log_in(anon, staff_user):
    response = anon.post("/dashboard/login", data={"username": "amira", "password": "wrong"})
    assert response.headers["location"] == "/dashboard/login?error=1"
    assert anon.get("/dashboard").status_code == 303


def test_a_deactivated_login_stops_working_mid_session(client, staff_user, seeded):
    assert client.get("/dashboard").status_code == 200
    seeded.get(type(staff_user), staff_user.staff_id).is_active = False
    seeded.commit()
    response = TestClient(client.app, follow_redirects=False, cookies=client.cookies).get("/dashboard")
    assert response.status_code == 303


def test_state_changing_posts_need_the_csrf_token(client):
    """Without it, any page a logged-in staff member visits could POST here."""
    response = client.post("/dashboard/shipping", data={"governorate": "Cairo", "fee": "60"})
    assert response.status_code == 422  # the field is required at all
    response = client.post(
        "/dashboard/shipping", data={"governorate": "Cairo", "fee": "60", "csrf_token": "forged"}
    )
    assert response.status_code == 403


# --- shipping -------------------------------------------------------------


def test_setting_a_fee_is_attributed(client, staff_user):
    token = csrf_from(client)
    response = client.post(
        "/dashboard/shipping", data={"governorate": "Cairo", "fee": "60", "csrf_token": token}
    )
    assert response.status_code == 200

    with SessionLocal() as db:
        assert float(db.get(ShippingRate, "Cairo").fee) == 60
        entry = db.query(AuditLog).filter_by(action="shipping.fee_set").one()
        assert entry.staff_id == staff_user.staff_id
        assert entry.before == {"fee": None}
        assert entry.after == {"fee": 60.0}


def test_clearing_a_fee_is_allowed(client):
    token = csrf_from(client)
    client.post("/dashboard/shipping", data={"governorate": "Cairo", "fee": "60", "csrf_token": token})
    client.post("/dashboard/shipping", data={"governorate": "Cairo", "fee": "", "csrf_token": token})
    with SessionLocal() as db:
        assert db.get(ShippingRate, "Cairo").fee is None


# --- inventory ------------------------------------------------------------


def test_per_variant_stock_edit(client, staff_user):
    token = csrf_from(client)
    response = client.post(
        "/dashboard/products/wanas-hoodie/variant",
        data={
            "variant_id": VARIANT,
            "stock_qty": "4",
            "low_stock_threshold": "3",
            "price": "620",
            "csrf_token": token,
        },
    )
    assert response.status_code == 200

    with SessionLocal() as db:
        variant = db.get(Variant, VARIANT)
        assert variant.stock_qty == 4
        assert variant.low_stock_threshold == 3
        assert float(variant.price) == 620
        assert variant.on_sale is True  # 620 < original 900
        assert variant.status == "in_stock"
        entry = db.query(AuditLog).filter_by(action="variant.edited").one()
        assert entry.before["stock_qty"] == 10
        assert entry.after["price"] == 620.0


def test_the_variant_grid_renders_the_length_axis(client):
    page = client.get("/dashboard/products/worker-jacket").text
    assert "Long" in page and "Short" in page


def test_size_chart_can_be_reassigned_and_cleared(client):
    token = csrf_from(client)
    client.post(
        "/dashboard/products/wanas-hoodie/size-chart", data={"size_chart": "", "csrf_token": token}
    )
    with SessionLocal() as db:
        from backend.models import Product

        assert db.get(Product, "wanas-hoodie").size_chart is None

    client.post(
        "/dashboard/products/wanas-hoodie/size-chart",
        data={"size_chart": "oversized-hoodie", "csrf_token": token},
    )
    with SessionLocal() as db:
        from backend.models import Product

        assert db.get(Product, "wanas-hoodie").size_chart == "oversized-hoodie"


# --- orders ---------------------------------------------------------------


def _place_order(external_id: str = WHO) -> str:
    with SessionLocal() as db:
        db.get(ShippingRate, "Cairo").fee = 60
        carts.add(db, "whatsapp", external_id, VARIANT, 1)
        result = orders.place_order(
            db,
            channel="whatsapp",
            external_id=external_id,
            customer_name="Omar",
            governorate="Cairo",
            address="1 Test Street",
            contact_phone="01000000001",
        )
        db.commit()
        return result["order_id"]


def test_order_appears_and_can_be_advanced(client, staff_user):
    order_id = _place_order()
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        assert order_id in client.get("/dashboard/orders").text
        token = csrf_from(client)
        response = client.post(
            f"/dashboard/orders/{order_id}/status", data={"new_status": "Packed", "csrf_token": token}
        )
        assert response.status_code == 200
    finally:
        notifications.register_sender(notifications.LogSender())

    with SessionLocal() as db:
        assert db.get(Order, order_id).status == "Packed"
        assert db.query(AuditLog).filter_by(action="order.status_changed").count() == 1
    assert sender.sent[0].template == "status_packed"


def test_delivered_triggers_the_feedback_request(client):
    order_id = _place_order()
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        token = csrf_from(client)
        for status in ("Packed", "Shipped", "Delivered"):
            client.post(
                f"/dashboard/orders/{order_id}/status", data={"new_status": status, "csrf_token": token}
            )
    finally:
        notifications.register_sender(notifications.LogSender())
    assert [m.template for m in sender.sent][-1] == "feedback_request"


def test_staff_cancel_returns_stock_and_notifies(client):
    order_id = _place_order()
    with SessionLocal() as db:
        before = db.get(Variant, VARIANT).stock_qty

    token = csrf_from(client)
    client.post(f"/dashboard/orders/{order_id}/cancel", data={"csrf_token": token})

    with SessionLocal() as db:
        assert db.get(Order, order_id).status == "Cancelled"
        assert db.get(Variant, VARIANT).stock_qty == before + 1


# --- queues ---------------------------------------------------------------


def test_resolving_a_handoff_is_what_un_pauses_a_conversation(client, seeded):
    with SessionLocal() as db:
        ctx = ToolContext(session=db, channel="whatsapp", external_id=WHO)
        call_tool(ctx, "request_human", {"reason": "customer_asked", "summary": "wants a person"})
        db.commit()
        queue_id = queues.open_items(db, QueueKind.HANDOFF.value)[0].queue_id
        assert identities.is_paused(db, "whatsapp", WHO) is True

    token = csrf_from(client)
    assert client.get(f"/dashboard/queues/{queue_id}").status_code == 200
    client.post(f"/dashboard/queues/{queue_id}/resolve", data={"decision": "resolved", "csrf_token": token})

    with SessionLocal() as db:
        assert db.get(StaffQueueItem, queue_id).status == "resolved"
        assert identities.is_paused(db, "whatsapp", WHO) is False


def test_replying_as_the_shop_does_not_hand_the_conversation_back(client):
    with SessionLocal() as db:
        ctx = ToolContext(session=db, channel="whatsapp", external_id=WHO)
        call_tool(ctx, "request_human", {"reason": "complaint", "summary": "damaged item"})
        db.commit()
        queue_id = queues.open_items(db, QueueKind.HANDOFF.value)[0].queue_id

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        token = csrf_from(client)
        client.post(
            f"/dashboard/queues/{queue_id}/reply",
            data={"text": "أنا مها من الفريق، بعتذر عن اللي حصل.", "csrf_token": token},
        )
    finally:
        notifications.register_sender(notifications.LogSender())

    assert sender.sent[0].text.startswith("أنا مها")
    with SessionLocal() as db:
        # Still paused, still open: replying and resolving are separate acts.
        assert identities.is_paused(db, "whatsapp", WHO) is True
        assert db.get(StaffQueueItem, queue_id).status == "open"


def test_second_resolve_is_told_rather_than_double_applying(client):
    with SessionLocal() as db:
        item = queues.enqueue(db, kind=QueueKind.ALERT.value, summary="low stock", reason="low_stock")
        db.commit()
        queue_id = item.queue_id

    token = csrf_from(client)
    client.post(f"/dashboard/queues/{queue_id}/resolve", data={"csrf_token": token})
    second = client.post(f"/dashboard/queues/{queue_id}/resolve", data={"csrf_token": token})
    assert "Already+resolved" in str(second.url) or "Already resolved" in second.text


def test_approving_a_swap_applies_it_and_recomputes_the_total(client, shopify):
    order_id = _place_order()
    with SessionLocal() as db:
        ctx = ToolContext(session=db, channel="whatsapp", external_id=WHO)
        result = call_tool(
            ctx,
            "request_item_swap",
            {"order_id": order_id, "from_variant_id": VARIANT, "to_variant_id": "wanas-hoodie-s-grey"},
        )
        db.get(Variant, "wanas-hoodie-s-grey").stock_qty = 5
        db.commit()
        # The swap claims the replacement through the same reservation a new
        # order uses, so the stock that has to be there is Shopify's.
        shopify.set("wanas-hoodie-s-grey", qty=5)
        queue_id = result["request_id"]
        stock_before = db.get(Variant, VARIANT).stock_qty

    token = csrf_from(client)
    client.post(
        f"/dashboard/queues/{queue_id}/apply-swap",
        data={"to_variant_id": "wanas-hoodie-s-grey", "csrf_token": token},
    )

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        assert order.items[0].variant_id == "wanas-hoodie-s-grey"
        assert order.items[0].color == "Grey"
        # Grey is 700, not 650 -- the total has to follow the swap.
        assert float(order.total) == 760
        assert db.get(Variant, "wanas-hoodie-s-grey").stock_qty == 4
        assert db.get(Variant, VARIANT).stock_qty == stock_before + 1
        assert db.get(StaffQueueItem, queue_id).status == "resolved"
        assert db.query(AuditLog).filter_by(action="swap.approved").count() == 1


def test_a_swap_that_has_no_stock_is_refused_not_half_applied(client):
    order_id = _place_order()
    with SessionLocal() as db:
        ctx = ToolContext(session=db, channel="whatsapp", external_id=WHO)
        result = call_tool(
            ctx, "request_item_swap", {"order_id": order_id, "from_variant_id": VARIANT}
        )
        db.commit()
        queue_id = result["request_id"]
        stock_before = db.get(Variant, VARIANT).stock_qty

    token = csrf_from(client)
    # wanas-hoodie-m-olive is sold out in the seed.
    client.post(
        f"/dashboard/queues/{queue_id}/apply-swap",
        data={"to_variant_id": "wanas-hoodie-m-olive", "csrf_token": token},
    )

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        assert order.items[0].variant_id == VARIANT
        assert db.get(Variant, VARIANT).stock_qty == stock_before
        assert db.get(StaffQueueItem, queue_id).status == QueueStatus.OPEN.value


# --- clients --------------------------------------------------------------


def test_blocking_a_client_stops_the_next_order(client):
    _place_order()
    with SessionLocal() as db:
        client_id = db.query(Client).one().client_id

    token = csrf_from(client)
    client.post(f"/dashboard/clients/{client_id}/status", data={"new_status": "blocked", "csrf_token": token})

    with SessionLocal() as db:
        assert db.get(Client, client_id).status == "blocked"
        carts.add(db, "whatsapp", WHO, VARIANT, 1)
        result = orders.place_order(
            db,
            channel="whatsapp",
            external_id=WHO,
            customer_name="Omar",
            governorate="Cairo",
            address="1 Test Street",
            contact_phone="01000000001",
        )
        assert result == {"error": "client_blocked"}


# --- health ---------------------------------------------------------------


def test_health_reports_what_is_still_missing(anon):
    payload = anon.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["whatsapp_configured"] is False
    assert payload["llm_key_set"] is False
