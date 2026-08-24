"""The Shopify Orders section of the staff dashboard.

Store-wide, not bot-only: a website order (no local `Order` row) has to show
up in the list next to a bot order, and staff actions have to route through
the right path for each -- the local order service (already transactional,
already notifies) when a local row exists, straight to Shopify when it does
not. See `dashboard/shopify_api.py`'s docstring.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import shopify_api
from dashboard import web as dashboard
from domain.models import Order, OrderStatus
from domain.services import (
    auth,
    carts,
    orders,
)

SECRET = "test-dashboard-secret"
VARIANT = "wanas-hoodie-s-olive"


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(shopify_api.router)
    return TestClient(app)


@pytest.fixture()
def staff(seeded):
    person = auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    return person


@pytest.fixture()
def logged_in(client, staff):
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


@pytest.fixture()
def bot_order(cairo_rate, seeded) -> Order:
    session = seeded
    carts.add(session, "whatsapp", "201555000111", VARIANT, 1)
    result = orders.place_order(
        session,
        channel="whatsapp",
        external_id="201555000111",
        customer_name="Hazem",
        governorate="Cairo",
        address="1 Test Street",
        contact_phone="01055566677",
    )
    assert "error" not in result, result
    # Fetched *before* the commit below, not after: `domain.db` opens every
    # SQLite transaction with BEGIN IMMEDIATE (a write lock) even for a read,
    # and a read issued after the commit would leave that lock held for the
    # rest of the test -- deadlocking the dashboard's own session_scope().
    order = session.get(Order, result["order_id"])
    session.commit()
    return order


@pytest.fixture()
def website_order(shopify) -> str:
    """An order placed on the storefront directly -- no local row at all."""
    return shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 650}],
        shipping_fee=60,
        customer_name="Website Customer",
        phone="01099988877",
        address="2 Storefront St",
        governorate="Giza",
    )


# --------------------------------------------------------------------------
# list / detail
# --------------------------------------------------------------------------


def test_orders_list_requires_login(client):
    assert client.get("/dashboard/api/shopify/orders").status_code == 401


def test_orders_list_shows_both_channels(logged_in, bot_order, website_order):
    res = logged_in.get("/dashboard/api/shopify/orders")
    assert res.status_code == 200
    by_id = {o["id"]: o for o in res.json()["orders"]}

    bot_entry = by_id[bot_order.shopify_order_id]
    assert bot_entry["source"] == "chatbot"
    assert bot_entry["local"] == {"order_id": bot_order.order_id, "channel": "whatsapp"}

    web_entry = by_id[website_order]
    assert web_entry["source"] == "website"
    assert web_entry["local"] is None


def test_order_detail_includes_fulfillment_orders(logged_in, bot_order):
    res = logged_in.get(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["fulfillment_orders"][0]["status"] == "OPEN"
    assert body["local"]["order_id"] == bot_order.order_id


def test_order_detail_404s_for_an_unknown_order(logged_in):
    res = logged_in.get("/dashboard/api/shopify/orders/gid://shopify/Order/999999")
    assert res.status_code == 404


def test_list_reports_an_outage_rather_than_an_empty_list(logged_in, shopify):
    shopify.down = True
    res = logged_in.get("/dashboard/api/shopify/orders")
    assert res.status_code == 503


# --------------------------------------------------------------------------
# fulfil
# --------------------------------------------------------------------------


def test_fulfilling_a_bot_order_succeeds(logged_in, bot_order):
    res = logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/fulfill", json={})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "SUCCESS"


def test_fulfilling_twice_is_refused_not_silently_repeated(logged_in, bot_order):
    first = logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/fulfill", json={})
    assert first.status_code == 200

    second = logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/fulfill", json={})
    assert second.status_code == 409
    assert second.json()["error"] == "already_fulfilled"


def test_fulfilling_an_unknown_order_404s(logged_in):
    res = logged_in.post("/dashboard/api/shopify/orders/gid://shopify/Order/999999/fulfill", json={})
    assert res.status_code == 404


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------


def test_cancelling_a_bot_order_uses_the_local_order_service(logged_in, bot_order, seeded):
    res = logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/cancel")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == OrderStatus.CANCELLED.value

    seeded.expire_all()
    assert seeded.get(Order, bot_order.order_id).status == OrderStatus.CANCELLED.value


def test_cancelling_a_website_order_calls_shopify_directly(logged_in, website_order, shopify):
    res = logged_in.post(f"/dashboard/api/shopify/orders/{website_order}/cancel")
    assert res.status_code == 200, res.text
    assert shopify.orders[website_order]["cancelled"] is True


def test_cancelling_an_already_cancelled_order_is_refused(logged_in, bot_order):
    first = logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/cancel")
    assert first.status_code == 200

    second = logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/cancel")
    assert second.status_code == 409


# --------------------------------------------------------------------------
# quantity edit
# --------------------------------------------------------------------------


def test_editing_quantity_on_a_bot_order_uses_the_local_order_service(logged_in, bot_order, seeded):
    res = logged_in.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/quantity",
        json={"variant_id": VARIANT, "quantity": 2},
    )
    assert res.status_code == 200, res.text

    seeded.expire_all()
    item = seeded.get(Order, bot_order.order_id).items[0]
    assert item.quantity == 2


def test_editing_quantity_on_a_website_order_calls_shopify_directly(logged_in, website_order, shopify):
    res = logged_in.post(
        f"/dashboard/api/shopify/orders/{website_order}/quantity",
        json={"variant_id": VARIANT, "quantity": 3},
    )
    assert res.status_code == 200, res.text
    assert shopify.orders[website_order]["lines"][VARIANT] == 3


def test_editing_quantity_rejects_a_missing_variant_id(logged_in, bot_order):
    res = logged_in.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/quantity",
        json={"quantity": 2},
    )
    assert res.status_code == 400
