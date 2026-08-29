"""The dashboard's two Customers views: Shopify's (store-wide) and the local
WhatsApp one -- kept separate on purpose, see both modules' docstrings.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import customers_api, shopify_api, web as dashboard
from domain.models import Order
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


def _ledger_order(customer_id, phone, *, total="100", cancelled=False, channel_hint="web",
                  governorate="Cairo", created_at="2026-08-01T00:00:00Z"):
    """One order in the shape `customer_ledger.index` reads.

    Only the keys the ledger folds -- the whole point of the module is that it
    reads `admin_orders.order_summary` dicts, so a test that hand-builds one
    builds only the fields that decide a total.
    """
    return {
        "id": f"gid://shopify/Order/{customer_id}-{total}-{created_at}",
        "customer_gid": f"gid://shopify/Customer/{customer_id}",
        "customer_phone": phone,
        "total": total,
        "cancelled": cancelled,
        "channel_hint": channel_hint,
        "governorate": governorate,
        "created_at": created_at,
    }


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
    # The order counts are folded out of the orders now, not read off the
    # customer record -- see `dashboard/customer_ledger.py`.
    monkeypatch.setattr(
        shopify_api.shopify_admin_orders, "list_all_orders",
        lambda *, query=None, max_pages=20: (
            [_ledger_order("1", "1", total="100")]
            + [_ledger_order("2", "2", total="100") for _ in range(9)],
            False,
        ),
    )

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


# --------------------------------------------------------------------------
# the shape Shopify actually sends
# --------------------------------------------------------------------------
#
# The fake shelf replaces `list_customers` wholesale, so `_summary` -- the
# function that reads Shopify's raw node -- never runs in any test above. That
# is exactly where the order-count filter broke in production: Shopify returns
# `numberOfOrders` as a *string*, and every filter comparing it to an int
# matched nothing. These tests go at the mapper directly, with the payload the
# live shop returns.


def test_the_order_count_is_read_as_a_number_not_the_string_shopify_sends():
    """`"1" == 1` is False, which is why "customers with exactly one order"
    returned none of the customers with exactly one order."""
    from integrations.shopify.admin_customers import _summary

    row = _summary(
        {
            "id": "gid://shopify/Customer/1",
            "displayName": "Hazem",
            "email": None,
            "phone": "+201067177128",
            "numberOfOrders": "3",
            "amountSpent": {"amount": "1200.00"},
            "defaultAddress": {"province": "Cairo"},
        }
    )

    assert row["order_count"] == 3
    assert isinstance(row["order_count"], int)


def test_a_customer_shopify_gives_no_count_for_is_zero_not_a_crash():
    from integrations.shopify.admin_customers import _summary

    row = _summary({"id": "gid://shopify/Customer/2", "numberOfOrders": None})

    assert row["order_count"] == 0


@pytest.mark.no_shopify
def test_the_filter_now_matches_what_shopify_sends(logged_in, monkeypatch):
    """End to end over the real mapper, which means the real `list_customers`
    -- hence `no_shopify`: the fake shelf replaces that function, and replacing
    it is what hid this bug in the first place. Only the transport is stood in
    for."""
    from integrations.shopify import admin_customers

    def fake_client(query, variables):
        return {
            "customers": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {"id": "gid://shopify/Customer/1", "displayName": "One",
                     "phone": "+201000000001", "email": None, "numberOfOrders": "1",
                     "amountSpent": {"amount": "100"}, "defaultAddress": {"province": "Cairo"}},
                    {"id": "gid://shopify/Customer/2", "displayName": "Two",
                     "phone": "+201000000002", "email": None, "numberOfOrders": "2",
                     "amountSpent": {"amount": "200"}, "defaultAddress": {"province": "Giza"}},
                ],
            }
        }

    monkeypatch.setattr(admin_customers, "get_admin_client", lambda: fake_client)
    monkeypatch.setattr(
        shopify_api.shopify_admin_orders, "list_all_orders",
        lambda *, query=None, max_pages=20: (
            [
                _ledger_order("1", "+201000000001"),
                _ledger_order("2", "+201000000002"),
                _ledger_order("2", "+201000000002", created_at="2026-08-02T00:00:00Z"),
            ],
            False,
        ),
    )

    body = logged_in.get("/dashboard/api/shopify/customers?orders_count=1").json()

    assert [c["name"] for c in body["customers"]] == ["One"]


# --------------------------------------------------------------------------
# "the whole store" means both sides
# --------------------------------------------------------------------------
#
# The store tab used to be Shopify's customer list alone, which was wrong by
# exactly the orders the bot placed before it attached a customer to them: the
# buyer is in wanas.db with a name and a governorate, and simply was not in the
# list that called itself everyone.


@pytest.fixture()
def website_order(shopify) -> str:
    """An order placed on the storefront directly -- no local row at all."""
    return shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 650}],
        shipping_fee=60,
        customer_name="Website Customer",
        phone="01044455566",
        address="2 Storefront St",
        governorate="Giza",
    )


@pytest.fixture()
def bot_only_customer(cairo_rate, seeded):
    """A `Client` whose phone no Shopify customer carries."""
    carts.add(seeded, "whatsapp", "201555000999", VARIANT, 1)
    result = orders.place_order(
        seeded,
        channel="whatsapp",
        external_id="201555000999",
        customer_name="Off Shopify",
        governorate="Cairo",
        address="9 Nowhere Street",
        contact_phone="01099988877",
    )
    assert "error" not in result, result
    seeded.commit()
    return result


def test_the_store_list_includes_a_bot_customer_shopify_never_recorded(
    logged_in, bot_only_customer, monkeypatch
):
    monkeypatch.setattr(
        shopify_api.shopify_admin_customers, "list_all_customers",
        lambda *, query=None: ([], False),
    )

    body = logged_in.get("/dashboard/api/shopify/customers").json()

    row = next(c for c in body["customers"] if c["name"] == "Off Shopify")
    assert row["source"] == "bot"
    assert row["governorate"] == "Cairo"
    # Their order exists in Shopify even though their *customer record* does
    # not, which is the whole shape of this case -- so the money is still
    # Shopify's number, folded out of the order, never summed from wanas.db.
    assert row["order_count"] == 1
    assert Decimal(row["amount_spent"]) > 0
    assert row["channels"] == ["whatsapp"]


def test_a_customer_on_both_sides_is_listed_once(logged_in, bot_only_customer, monkeypatch):
    """Matched on the phone, normalised the same way the order path does, so
    `01099988877` and `+201099988877` are one person and not two rows."""
    monkeypatch.setattr(
        shopify_api.shopify_admin_customers, "list_all_customers",
        lambda *, query=None: ([{
            "id": "gid://shopify/Customer/7", "name": "Off Shopify",
            "phone": "+201099988877", "email": None, "order_count": 4,
            "amount_spent": "900.00", "governorate": "Cairo",
        }], False),
    )

    body = logged_in.get("/dashboard/api/shopify/customers").json()

    matching = [c for c in body["customers"] if c["name"] == "Off Shopify"]
    assert len(matching) == 1
    # Shopify's row wins, and its count comes from the orders rather than from
    # the `order_count` the customer record carried: one order placed once is
    # one order, however many times the same person appears in two lists.
    assert matching[0]["source"] == "shopify"
    assert matching[0]["order_count"] == 1


# --------------------------------------------------------------------------
# the bot tab answers the same questions
# --------------------------------------------------------------------------


def test_the_bot_list_carries_an_order_count(logged_in, bot_order):
    body = logged_in.get("/dashboard/api/customers").json()

    row = next(c for c in body["customers"] if c["full_name"] == "Hazem")
    assert row["order_count"] == 1


def test_the_bot_list_filters_by_order_count(logged_in, bot_order):
    assert logged_in.get("/dashboard/api/customers?orders_count=1").json()["customers"]
    assert logged_in.get("/dashboard/api/customers?orders_count=5").json()["customers"] == []


def test_the_bot_list_filters_by_governorate(logged_in, bot_order):
    hit = logged_in.get("/dashboard/api/customers?governorate=Cairo").json()
    miss = logged_in.get("/dashboard/api/customers?governorate=Giza").json()
    assert [c["full_name"] for c in hit["customers"]] == ["Hazem"]
    assert miss["customers"] == []


def test_the_bot_list_offers_the_shops_governorates(logged_in, bot_order):
    body = logged_in.get("/dashboard/api/customers").json()
    assert any(g["key"] == "Cairo" for g in body["governorates"])


def test_the_bot_list_refuses_a_sort_it_does_not_have(logged_in):
    assert logged_in.get("/dashboard/api/customers?sort=whatever").status_code == 400


# --------------------------------------------------------------------------
# one list, three tabs
# --------------------------------------------------------------------------
#
# The bot tab and the store tab used to be two routes returning two different
# customer dicts, so switching tab changed which columns existed. They are one
# list filtered three ways now, which is what the owner asked for: "the chat
# customers or web customers or the whole -- the same shape".


def test_the_three_tabs_are_the_same_shape(logged_in, bot_order, website_order):
    shapes = []
    for segment in ("all", "bot", "web"):
        body = logged_in.get(f"/dashboard/api/shopify/customers?segment={segment}").json()
        assert body["customers"], segment
        shapes.append(set(body["customers"][0]))

    assert shapes[0] == shapes[1] == shapes[2]
    assert {"order_count", "amount_spent", "cancelled_count", "cancelled_amount",
            "channels", "governorate"} <= shapes[0]


def test_the_bot_tab_holds_the_conversation_buyers_and_the_web_tab_the_rest(
    logged_in, bot_order, website_order
):
    bot = logged_in.get("/dashboard/api/shopify/customers?segment=bot").json()["customers"]
    web = logged_in.get("/dashboard/api/shopify/customers?segment=web").json()["customers"]

    assert all("whatsapp" in c["channels"] or c["source"] == "bot" for c in bot)
    assert all("web" in c["channels"] for c in web)
    assert {c["name"] for c in bot} != {c["name"] for c in web}


def test_an_unknown_segment_is_refused_rather_than_read_as_everyone(logged_in):
    assert logged_in.get("/dashboard/api/shopify/customers?segment=vip").status_code == 400


# --------------------------------------------------------------------------
# the four numbers, and the channels
# --------------------------------------------------------------------------


def test_a_cancelled_order_leaves_the_orders_column_and_enters_the_cancelled_one(
    logged_in, bot_order, seeded
):
    from domain.services import orders as orders_service

    before = logged_in.get("/dashboard/api/shopify/customers").json()["customers"]
    row = next(c for c in before if c["order_count"] == 1)

    order = seeded.get(Order, bot_order["order_id"])
    assert "error" not in orders_service.cancel(seeded, order, by="staff", notify_customer=False)
    seeded.commit()

    after = next(
        c for c in logged_in.get("/dashboard/api/shopify/customers").json()["customers"]
        if c["phone"] == row["phone"]
    )
    assert after["order_count"] == 0
    assert after["amount_spent"] == "0.00"
    assert after["cancelled_count"] == 1
    assert Decimal(after["cancelled_amount"]) == Decimal(row["amount_spent"])


def test_the_row_says_which_channels_the_customer_bought_through(logged_in, bot_order):
    body = logged_in.get("/dashboard/api/shopify/customers").json()

    row = next(c for c in body["customers"] if c["order_count"] == 1)
    assert row["channels"] == ["whatsapp"]


# --------------------------------------------------------------------------
# the drawer
# --------------------------------------------------------------------------


def test_the_customer_drawer_shows_orders_the_way_the_orders_screen_does(
    logged_in, bot_order, shopify
):
    listed = logged_in.get("/dashboard/api/shopify/orders").json()["orders"][0]
    customers = logged_in.get("/dashboard/api/shopify/customers").json()["customers"]
    gid = next(c["id"] for c in customers if str(c["id"]).startswith("gid://"))

    body = logged_in.get(f"/dashboard/api/shopify/customers/{gid}").json()

    assert body["orders"], body
    order = body["orders"][0]
    # Same keys, so one renderer draws both tables and they cannot disagree
    # about whether an order was cancelled.
    assert {"cancelled", "fulfillment_status", "payment_method", "channel", "total"} <= set(order)
    assert set(order) >= set(listed) - {"local", "source"}


def test_the_drawers_totals_are_the_drawers_own_orders(logged_in, bot_order, shopify):
    customers = logged_in.get("/dashboard/api/shopify/customers").json()["customers"]
    gid = next(c["id"] for c in customers if str(c["id"]).startswith("gid://"))

    body = logged_in.get(f"/dashboard/api/shopify/customers/{gid}").json()

    standing = [o for o in body["orders"] if not o["cancelled"]]
    assert body["order_count"] == len(standing)
    assert Decimal(body["amount_spent"]) == sum(Decimal(o["total"]) for o in standing)


def test_the_bot_drawer_carries_the_same_numbers_and_the_same_order_shape(
    logged_in, bot_order, seeded
):
    """The bot side reads Postgres and the store side reads Shopify, and both
    drawers are drawn by one function -- so both have to arrive shaped alike."""
    client_id = seeded.get(Order, bot_order["order_id"]).client_id
    seeded.commit()  # let go of the write lock before the route opens its own

    body = logged_in.get(f"/dashboard/api/customers/{client_id}").json()

    assert {"order_count", "amount_spent", "cancelled_count", "cancelled_amount",
            "channels"} <= set(body)
    order = body["orders"][0]
    assert {"cancelled", "fulfillment_status", "payment_method", "channel", "total"} <= set(order)
    assert order["channel"] == "whatsapp"


def test_one_person_holding_two_shopify_records_is_one_row(logged_in, monkeypatch):
    """The live shop has exactly this: a website checkout made a fresh
    customer with no phone, beside the record the backfill built for the same
    buyer out of their bot orders. Both resolve to one set of totals through
    the phone on the shipping address, so listing both would show the same
    orders twice under two spellings of one name."""
    monkeypatch.setattr(
        shopify_api.shopify_admin_customers, "list_all_customers",
        lambda *, query=None: ([
            {"id": "gid://shopify/Customer/1", "name": "Mohamed Fathy",
             "phone": "+201021233010", "email": None, "governorate": "Monufia"},
            {"id": "gid://shopify/Customer/2", "name": "mohmed fathy",
             "phone": None, "email": None, "governorate": None},
        ], False),
    )
    monkeypatch.setattr(
        shopify_api.shopify_admin_orders, "list_all_orders",
        lambda *, query=None, max_pages=20: (
            [
                _ledger_order("1", "+201021233010", channel_hint="whatsapp"),
                # The website order: a different customer record, the same
                # phone on the address.
                dict(_ledger_order("2", "+201021233010", channel_hint="web"),
                     id="gid://shopify/Order/web"),
            ],
            False,
        ),
    )

    rows = logged_in.get("/dashboard/api/shopify/customers").json()["customers"]

    assert len(rows) == 1
    # The record carrying the phone is the one kept -- it is what every merge
    # here, and the backfill itself, keys on.
    assert rows[0]["id"] == "gid://shopify/Customer/1"
    assert rows[0]["order_count"] == 2
    assert rows[0]["channels"] == ["whatsapp", "web"]
