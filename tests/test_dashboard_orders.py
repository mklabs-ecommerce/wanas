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
from dashboard import shopify_api, web as dashboard
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


# --------------------------------------------------------------------------
# the Orders view's three toggles: payment, customer, channel
#
# All three narrow the same list, and all three are classified once on the
# server -- see `integrations/shopify/admin_orders._payment_method` and
# `dashboard/shopify_api._order_channel`. The point of testing them here
# rather than only in the classifier is that a filter that silently means
# "all" when it does not understand its argument is exactly as wrong as one
# that filters on the wrong field, and only an endpoint test catches it.
# --------------------------------------------------------------------------


@pytest.fixture()
def cod_website_order(shopify) -> str:
    """A storefront order paid cash on delivery -- so "COD" and "from the
    bot" are provably different questions, not the same one twice."""
    return shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 400}],
        shipping_fee=60,
        customer_name="COD Website Customer",
        phone="01088877766",
        address="3 Storefront St",
        governorate="Giza",
        payment_gateways=["Cash on Delivery (COD)"],
    )


@pytest.fixture()
def gatewayless_order(shopify) -> str:
    """No gateway at all: the third bucket. Never "online"."""
    return shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 300}],
        shipping_fee=60,
        customer_name="Draft Customer",
        phone="01077766655",
        address="4 Storefront St",
        governorate="Giza",
        payment_gateways=[],
    )


def test_a_bot_order_is_classified_cash_on_delivery(logged_in, bot_order):
    entry = next(
        o for o in logged_in.get("/dashboard/api/shopify/orders").json()["orders"]
        if o["id"] == bot_order.shopify_order_id
    )
    assert entry["payment_method"] == "cod"


def test_an_order_with_no_gateway_is_unknown_not_online(logged_in, gatewayless_order):
    entry = next(
        o for o in logged_in.get("/dashboard/api/shopify/orders").json()["orders"]
        if o["id"] == gatewayless_order
    )
    assert entry["payment_method"] == "unknown"


def test_payment_filter_separates_cod_from_online(
    logged_in, bot_order, website_order, cod_website_order, gatewayless_order
):
    cod = {o["id"] for o in logged_in.get("/dashboard/api/shopify/orders?payment=cod").json()["orders"]}
    assert cod == {bot_order.shopify_order_id, cod_website_order}

    online = {o["id"] for o in logged_in.get("/dashboard/api/shopify/orders?payment=online").json()["orders"]}
    assert online == {website_order}

    unknown = {
        o["id"] for o in logged_in.get("/dashboard/api/shopify/orders?payment=unknown").json()["orders"]
    }
    assert unknown == {gatewayless_order}

    everything = logged_in.get("/dashboard/api/shopify/orders?payment=all").json()["orders"]
    assert len(everything) == 4


def test_an_unknown_payment_filter_is_refused_not_ignored(logged_in):
    res = logged_in.get("/dashboard/api/shopify/orders?payment=bitcoin")
    assert res.status_code == 400


def test_channel_comes_from_the_local_row_not_the_shopify_tags(logged_in, bot_order, website_order):
    by_id = {o["id"]: o for o in logged_in.get("/dashboard/api/shopify/orders").json()["orders"]}
    assert by_id[bot_order.shopify_order_id]["channel"] == "whatsapp"
    assert by_id[website_order]["channel"] == "web"


def test_channel_filter_narrows_the_list(logged_in, bot_order, website_order):
    web = logged_in.get("/dashboard/api/shopify/orders?channel=web").json()["orders"]
    assert {o["id"] for o in web} == {website_order}

    whatsapp = logged_in.get("/dashboard/api/shopify/orders?channel=whatsapp").json()["orders"]
    assert {o["id"] for o in whatsapp} == {bot_order.shopify_order_id}

    instagram = logged_in.get("/dashboard/api/shopify/orders?channel=instagram_dm").json()["orders"]
    assert instagram == []


def test_a_first_time_buyer_is_new_and_a_second_order_makes_them_returning(
    logged_in, shopify, cairo_rate, seeded
):
    first = shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 500}],
        shipping_fee=60, customer_name="Repeat", phone="01011122233", governorate="Cairo",
    )
    listed = {o["id"]: o for o in logged_in.get("/dashboard/api/shopify/orders").json()["orders"]}
    assert listed[first]["customer_kind"] == "new"
    assert listed[first]["customer_order_count"] == 1

    shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 500}],
        shipping_fee=60, customer_name="Repeat", phone="01011122233", governorate="Cairo",
    )
    listed = {o["id"]: o for o in logged_in.get("/dashboard/api/shopify/orders").json()["orders"]}
    assert listed[first]["customer_kind"] == "returning"

    returning = logged_in.get("/dashboard/api/shopify/orders?customer=returning").json()["orders"]
    assert len(returning) == 2
    assert logged_in.get("/dashboard/api/shopify/orders?customer=new").json()["orders"] == []


def test_an_unknown_customer_filter_is_refused(logged_in):
    assert logged_in.get("/dashboard/api/shopify/orders?customer=loyal").status_code == 400


def test_filters_combine_rather_than_replace_each_other(
    logged_in, bot_order, website_order, cod_website_order
):
    """COD *and* from the website -- neither toggle may quietly win."""
    res = logged_in.get("/dashboard/api/shopify/orders?payment=cod&channel=web")
    assert {o["id"] for o in res.json()["orders"]} == {cod_website_order}


def _fake_order(gid, *, payment, kind="new", governorate="Cairo"):
    return {
        "id": gid, "name": gid, "created_at": "2026-01-05T00:00:00Z",
        "financial_status": "PENDING", "fulfillment_status": "UNFULFILLED",
        "cancelled": False, "tags": [], "customer_name": "Someone",
        "customer_phone": gid, "customer_order_count": 2 if kind == "returning" else 1,
        "customer_kind": kind, "payment_gateways": [], "payment_method": payment,
        "governorate": governorate, "total": "100", "line_items": [], "source": "website",
    }


def test_filtering_walks_every_page_not_only_the_first(logged_in, monkeypatch):
    """The bug this guards is the one already fixed for customers: filtering
    page one makes "عند الاستلام" mean "the COD orders among the last fifty
    orders", and the KPI strip above the table totals that subset while
    reading like a store figure."""
    pages = [
        {"orders": [_fake_order("p1-online", payment="online")],
         "has_next_page": True, "end_cursor": "cursor-1"},
        {"orders": [_fake_order("p2-cod", payment="cod")],
         "has_next_page": False, "end_cursor": None},
    ]

    def fake_list(*, query=None, cursor=None):
        return pages[0] if cursor is None else pages[1]

    monkeypatch.setattr(shopify_api.shopify_admin_orders, "list_orders", fake_list)

    body = logged_in.get("/dashboard/api/shopify/orders?payment=cod").json()
    assert [o["id"] for o in body["orders"]] == ["p2-cod"]
    assert body["truncated"] is False


def test_an_unfiltered_list_still_reads_a_single_page(logged_in, monkeypatch):
    """Nothing pages through the whole shop just to render the default view --
    the cost is paid only when a toggle actually needs it."""
    calls = []

    def fake_list(*, query=None, cursor=None):
        calls.append(cursor)
        return {"orders": [_fake_order("only", payment="cod")],
                "has_next_page": True, "end_cursor": "more"}

    monkeypatch.setattr(shopify_api.shopify_admin_orders, "list_orders", fake_list)
    body = logged_in.get("/dashboard/api/shopify/orders").json()
    assert calls == [None]
    assert body["has_next_page"] is True


def test_hitting_the_page_cap_while_filtering_is_reported(logged_in, monkeypatch):
    def endless(*, query=None, cursor=None):
        return {"orders": [_fake_order("x", payment="cod")],
                "has_next_page": True, "end_cursor": "more"}

    monkeypatch.setattr(shopify_api.shopify_admin_orders, "list_orders", endless)
    body = logged_in.get("/dashboard/api/shopify/orders?payment=cod").json()
    assert body["truncated"] is True
    assert body["has_next_page"] is False


# --------------------------------------------------------------------------
# orders Shopify has no customer record for
# --------------------------------------------------------------------------
#
# Every order the bot placed before it started attaching a customer
# (`shopify_orders._customer`) has a shipping address and nothing else, and no
# call can give it one afterwards -- `orderUpdate` has no customer field. The
# list has to read the name off the address for those, without inventing the
# rest of a customer record around it.


def _node_without_customer(**overrides):
    node = {
        "id": "gid://shopify/Order/9001",
        "name": "#1028",
        "createdAt": "2026-08-26T10:00:00Z",
        "displayFinancialStatus": "PENDING",
        "displayFulfillmentStatus": "UNFULFILLED",
        "tags": ["chatbot", "whatsapp", "cash-on-delivery"],
        "paymentGatewayNames": ["Cash on Delivery (COD)"],
        "customer": None,
        "shippingAddress": {
            "name": "حازم عبد الحميد",
            "phone": "+201067177128",
            "city": "Cairo",
            "province": "Cairo",
        },
        "totalPriceSet": {"shopMoney": {"amount": "860.00", "currencyCode": "EGP"}},
        "lineItems": {"nodes": []},
    }
    node.update(overrides)
    return node


def test_an_order_with_no_customer_record_shows_the_name_off_the_address():
    row = shopify_api.shopify_admin_orders._order_summary(_node_without_customer())

    assert row["customer_name"] == "حازم عبد الحميد"
    assert row["customer_phone"] == "+201067177128"


def test_a_name_read_off_the_address_is_not_evidence_of_a_first_order():
    """The name is display; the count is a claim. An address cannot say
    whether this person has bought here before, so the row stays `unknown`
    rather than being folded in with genuine first-time buyers."""
    row = shopify_api.shopify_admin_orders._order_summary(_node_without_customer())

    assert row["customer_order_count"] is None
    assert row["customer_kind"] == "unknown"


def test_a_real_customer_record_still_wins_over_the_address():
    node = _node_without_customer(
        customer={
            "id": "gid://shopify/Customer/5",
            "displayName": "Hazem A.",
            "phone": "+201000000001",
            "numberOfOrders": "3",
        }
    )

    row = shopify_api.shopify_admin_orders._order_summary(node)

    assert row["customer_name"] == "Hazem A."
    assert row["customer_phone"] == "+201000000001"
    assert row["customer_kind"] == "returning"
