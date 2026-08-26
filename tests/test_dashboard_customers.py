"""The dashboard's two Customers views: Shopify's (store-wide) and the local
WhatsApp one -- kept separate on purpose, see both modules' docstrings.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import customers_api, shopify_api, web as dashboard
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


# --------------------------------------------------------------------------
# the Customers view's filters: order count, governorate, and the sort
#
# Every one of these is applied to *every* matching customer, not to the first
# page of them -- see `integrations/shopify/admin_customers.list_all_customers`.
# The pagination test below is the one that matters: "the customer with the
# most orders", computed over page one, is the most of that page, and nothing
# on screen would have said so.
# --------------------------------------------------------------------------


@pytest.fixture()
def three_buyers(shopify):
    """One customer with three orders, one with two, one with a single one --
    each in a different governorate."""
    def buy(phone, name, governorate, times):
        for _ in range(times):
            shopify.seed_order(
                items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 500}],
                shipping_fee=60, customer_name=name, phone=phone, governorate=governorate,
            )

    buy("01000000003", "Three Timer", "Cairo", 3)
    buy("01000000002", "Two Timer", "Giza", 2)
    buy("01000000001", "One Timer", "Cairo", 1)


def _named(body):
    return {c["name"]: c["order_count"] for c in body["customers"]}


def test_filtering_by_an_exact_order_count(logged_in, three_buyers):
    body = logged_in.get("/dashboard/api/shopify/customers?orders_count=2").json()
    assert _named(body) == {"Two Timer": 2}


def test_filtering_by_at_least_an_order_count(logged_in, three_buyers):
    body = logged_in.get("/dashboard/api/shopify/customers?orders_count=2&orders_op=gte").json()
    assert _named(body) == {"Three Timer": 3, "Two Timer": 2}


def test_filtering_by_governorate(logged_in, three_buyers):
    body = logged_in.get("/dashboard/api/shopify/customers?governorate=Cairo").json()
    assert set(_named(body)) == {"Three Timer", "One Timer"}


def test_a_customer_with_no_governorate_is_findable_rather_than_lost(logged_in, shopify):
    shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 500}],
        shipping_fee=60, customer_name="Addressless", phone="01000000009", governorate="",
    )
    body = logged_in.get("/dashboard/api/shopify/customers?governorate=__unknown__").json()
    assert set(_named(body)) == {"Addressless"}


def test_sorting_by_order_count_both_ways(logged_in, three_buyers):
    desc = logged_in.get("/dashboard/api/shopify/customers?sort=orders_desc").json()
    assert [c["order_count"] for c in desc["customers"]] == [3, 2, 1]

    asc = logged_in.get("/dashboard/api/shopify/customers?sort=orders_asc").json()
    assert [c["order_count"] for c in asc["customers"]] == [1, 2, 3]


def test_the_governorate_dropdown_comes_from_the_shops_own_list(logged_in, three_buyers):
    """Not from whatever rows happened to load -- otherwise the filter changes
    shape every time the list does, and never offers a governorate the shop
    ships to but has not sold to yet."""
    body = logged_in.get("/dashboard/api/shopify/customers").json()
    keys = {g["key"] for g in body["governorates"]}
    assert "Cairo" in keys and "Giza" in keys
    assert len(keys) > 3
    assert all(g["label_ar"] for g in body["governorates"])


def test_a_bad_sort_or_operator_is_refused(logged_in):
    assert logged_in.get("/dashboard/api/shopify/customers?sort=alphabetical").status_code == 400
    assert logged_in.get("/dashboard/api/shopify/customers?orders_op=lte").status_code == 400


def test_filtering_walks_every_page_not_only_the_first(logged_in, monkeypatch):
    """The bug this guards: the top customer by orders may not be on page one
    at all, and a page-one-only filter looks like it works."""
    pages = [
        {
            "customers": [
                {"id": "1", "name": "Page One", "order_count": 1, "governorate": "Cairo",
                 "amount_spent": "100", "email": None, "phone": "1"}
            ],
            "has_next_page": True,
            "end_cursor": "cursor-1",
        },
        {
            "customers": [
                {"id": "2", "name": "Page Two", "order_count": 9, "governorate": "Cairo",
                 "amount_spent": "900", "email": None, "phone": "2"}
            ],
            "has_next_page": False,
            "end_cursor": None,
        },
    ]

    def fake_list(*, query=None, cursor=None):
        return pages[0] if cursor is None else pages[1]

    monkeypatch.setattr(shopify_api.shopify_admin_customers, "list_customers", fake_list)

    body = logged_in.get("/dashboard/api/shopify/customers?sort=orders_desc").json()
    assert [c["name"] for c in body["customers"]] == ["Page Two", "Page One"]
    assert body["truncated"] is False


def test_hitting_the_page_cap_is_reported_rather_than_hidden(logged_in, monkeypatch):
    def endless(*, query=None, cursor=None):
        return {
            "customers": [
                {"id": "x", "name": "Someone", "order_count": 1, "governorate": "Cairo",
                 "amount_spent": "1", "email": None, "phone": "x"}
            ],
            "has_next_page": True,
            "end_cursor": "more",
        }

    monkeypatch.setattr(shopify_api.shopify_admin_customers, "list_customers", endless)
    body = logged_in.get("/dashboard/api/shopify/customers?sort=orders_desc").json()
    assert body["truncated"] is True
