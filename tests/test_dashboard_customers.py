"""The dashboard's two Customers views: Shopify's (store-wide) and the local
WhatsApp one -- kept separate on purpose, see both modules' docstrings.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings
from backend.services import auth, carts, orders
from dashboard import customers_api, shopify_api
from dashboard import web as dashboard

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
    app.include_router(customers_api.router)
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
def bot_order(cairo_rate, seeded):
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
    session.commit()
    return result


# --------------------------------------------------------------------------
# Shopify customers (store-wide)
# --------------------------------------------------------------------------


def test_shopify_customers_list_requires_login(client):
    assert client.get("/dashboard/api/shopify/customers").status_code == 401


def test_shopify_customers_list_includes_a_bot_order_customer(logged_in, bot_order):
    res = logged_in.get("/dashboard/api/shopify/customers")
    assert res.status_code == 200
    names = {c["name"] for c in res.json()["customers"]}
    assert "Hazem" in names


def test_shopify_customer_detail_lists_their_orders(logged_in, bot_order, shopify):
    customers = logged_in.get("/dashboard/api/shopify/customers").json()["customers"]
    gid = next(c["id"] for c in customers if c["name"] == "Hazem")
    res = logged_in.get(f"/dashboard/api/shopify/customers/{gid}")
    assert res.status_code == 200
    assert len(res.json()["orders"]) == 1


def test_shopify_customer_detail_404s_for_an_unknown_customer(logged_in):
    res = logged_in.get("/dashboard/api/shopify/customers/gid://shopify/Customer/999999")
    assert res.status_code == 404


def test_shopify_customers_reports_an_outage(logged_in, shopify):
    shopify.down = True
    assert logged_in.get("/dashboard/api/shopify/customers").status_code == 503


# --------------------------------------------------------------------------
# WhatsApp customers (local Client rows)
# --------------------------------------------------------------------------


def test_local_customers_list_requires_login(client):
    assert client.get("/dashboard/api/customers").status_code == 401


def test_local_customers_list_includes_a_bot_customer(logged_in, bot_order):
    res = logged_in.get("/dashboard/api/customers")
    assert res.status_code == 200
    names = {c["full_name"] for c in res.json()["customers"]}
    assert "Hazem" in names


def test_local_customers_search_filters_by_name(logged_in, bot_order):
    res = logged_in.get("/dashboard/api/customers?q=Hazem")
    assert res.status_code == 200
    assert len(res.json()["customers"]) == 1

    miss = logged_in.get("/dashboard/api/customers?q=Nobody")
    assert miss.json()["customers"] == []


def test_local_customer_detail_lists_their_orders(logged_in, bot_order):
    listing = logged_in.get("/dashboard/api/customers").json()["customers"]
    client_id = next(c["client_id"] for c in listing if c["full_name"] == "Hazem")
    res = logged_in.get(f"/dashboard/api/customers/{client_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["orders"][0]["order_id"] == bot_order["order_id"]


def test_local_customer_detail_404s_for_an_unknown_client(logged_in):
    res = logged_in.get("/dashboard/api/customers/999999")
    assert res.status_code == 404


def test_a_website_only_order_never_creates_a_local_client(logged_in, shopify):
    """The whole point of keeping the two customer views separate: a sale
    with no bot involvement must not appear in the WhatsApp-side list."""
    shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 650}],
        shipping_fee=60,
        customer_name="Website Only Person",
        phone="01099988877",
    )
    res = logged_in.get("/dashboard/api/customers")
    names = {c["full_name"] for c in res.json()["customers"]}
    assert "Website Only Person" not in names
