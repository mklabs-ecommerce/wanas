"""The `item_swap` and `alert` review-queue panel -- the other half of
`request_human`'s dashboard, for the two queue kinds that had full backend
logic and no UI at all before this. See `dashboard/queue_api.py`.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import queue_api
from dashboard import web as dashboard
from domain.models import Order, OrderStatus
from domain.services import (
    auth,
    carts,
    orders,
)
from domain.services.notifications import item_swap_requested

SECRET = "test-dashboard-secret"
VARIANT_A = "wanas-hoodie-s-olive"
VARIANT_B = "wanas-hoodie-s-black"  # also in stock in the seeded catalog, unlike most other olive sizes


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(queue_api.router)
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
    carts.add(session, "whatsapp", "201555000111", VARIANT_A, 1)
    result = orders.place_order(
        session, channel="whatsapp", external_id="201555000111", customer_name="Hazem",
        governorate="Cairo", address="1 Test Street", contact_phone="01055566677",
    )
    assert "error" not in result, result
    order = session.get(Order, result["order_id"])
    session.commit()
    return order


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------


def test_queue_list_requires_login(client):
    assert client.get("/dashboard/api/queue").status_code == 401


def test_queue_list_shows_alerts_and_swaps_together(logged_in, bot_order, seeded):
    item_swap_requested(
        seeded, bot_order,
        {"channel": "whatsapp", "external_id": "201555000111", "from_variant_id": VARIANT_A, "to_variant_id": VARIANT_B},
        f"{bot_order.order_id}: swap request",
    )
    seeded.commit()

    res = logged_in.get("/dashboard/api/queue")
    assert res.status_code == 200
    kinds = {i["kind"] for i in res.json()["items"]}
    assert "item_swap" in kinds
    # order_confirmed already enqueued an alert when the fixture placed the order.
    assert "alert" in kinds


def test_queue_list_can_filter_by_kind(logged_in, bot_order, seeded):
    item_swap_requested(
        seeded, bot_order,
        {"channel": "whatsapp", "external_id": "201555000111", "from_variant_id": VARIANT_A, "to_variant_id": VARIANT_B},
        "swap",
    )
    seeded.commit()

    res = logged_in.get("/dashboard/api/queue?kind=item_swap")
    items = res.json()["items"]
    assert all(i["kind"] == "item_swap" for i in items)
    assert len(items) == 1


# --------------------------------------------------------------------------
# resolve (alerts, or a declined swap)
# --------------------------------------------------------------------------


def test_resolving_an_alert_removes_it_from_the_open_list(logged_in, bot_order, seeded):
    before = logged_in.get("/dashboard/api/queue?kind=alert").json()["items"]
    assert len(before) == 1
    queue_id = before[0]["queue_id"]

    res = logged_in.post(f"/dashboard/api/queue/{queue_id}/resolve")
    assert res.status_code == 200

    after = logged_in.get("/dashboard/api/queue?kind=alert").json()["items"]
    assert after == []


def test_resolving_an_already_resolved_item_is_refused(logged_in, bot_order, seeded):
    queue_id = logged_in.get("/dashboard/api/queue?kind=alert").json()["items"][0]["queue_id"]
    logged_in.post(f"/dashboard/api/queue/{queue_id}/resolve")

    second = logged_in.post(f"/dashboard/api/queue/{queue_id}/resolve")
    assert second.status_code == 409


# --------------------------------------------------------------------------
# approve-swap
# --------------------------------------------------------------------------


def test_approving_a_swap_applies_it_and_resolves_the_queue_item(logged_in, bot_order, seeded):
    request_id = item_swap_requested(
        seeded, bot_order,
        {"channel": "whatsapp", "external_id": "201555000111", "from_variant_id": VARIANT_A, "to_variant_id": VARIANT_B},
        "swap",
    )
    seeded.commit()

    res = logged_in.post(f"/dashboard/api/queue/{request_id}/approve-swap")
    assert res.status_code == 200, res.text

    seeded.expire_all()
    order = seeded.get(Order, bot_order.order_id)
    assert order.items[0].variant_id == VARIANT_B
    assert order.status == OrderStatus.CONFIRMED.value
    # That read opened its own transaction (SQLite BEGIN IMMEDIATE, a write
    # lock, on this connection); release it before the next HTTP call opens
    # its own session_scope(), or the two deadlock against each other.
    seeded.rollback()

    still_open = logged_in.get("/dashboard/api/queue?kind=item_swap").json()["items"]
    assert still_open == []


def test_approving_a_swap_with_no_replacement_named_requires_one_in_the_request(logged_in, bot_order, seeded):
    request_id = item_swap_requested(
        seeded, bot_order,
        {"channel": "whatsapp", "external_id": "201555000111", "from_variant_id": VARIANT_A, "to_variant_id": None},
        "swap, no replacement named yet",
    )
    seeded.commit()

    missing = logged_in.post(f"/dashboard/api/queue/{request_id}/approve-swap")
    assert missing.status_code == 400

    supplied = logged_in.post(
        f"/dashboard/api/queue/{request_id}/approve-swap", json={"to_variant_id": VARIANT_B}
    )
    assert supplied.status_code == 200, supplied.text


def test_approving_a_swap_out_of_stock_leaves_the_queue_item_open(logged_in, bot_order, seeded, shopify):
    shopify.set(VARIANT_B, qty=0)
    request_id = item_swap_requested(
        seeded, bot_order,
        {"channel": "whatsapp", "external_id": "201555000111", "from_variant_id": VARIANT_A, "to_variant_id": VARIANT_B},
        "swap",
    )
    seeded.commit()

    res = logged_in.post(f"/dashboard/api/queue/{request_id}/approve-swap")
    assert res.status_code == 409

    still_open = logged_in.get("/dashboard/api/queue?kind=item_swap").json()["items"]
    assert len(still_open) == 1


def test_approving_an_unknown_queue_item_is_refused(logged_in):
    res = logged_in.post("/dashboard/api/queue/SWAP-999999/approve-swap")
    assert res.status_code == 409
