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


def test_a_second_order_is_returning_and_the_first_one_stays_new(
    logged_in, shopify, cairo_rate, seeded
):
    """New/returning describes the *order*, not the customer as they are today.

    Read off Shopify's lifetime `numberOfOrders` it described the customer, so
    buying a second time retroactively relabelled the first order "returning"
    too -- and the new-customer count shrank on a range whose orders had not
    changed.
    """
    first = shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 500}],
        shipping_fee=60, customer_name="Repeat", phone="01011122233", governorate="Cairo",
    )
    listed = {o["id"]: o for o in logged_in.get("/dashboard/api/shopify/orders").json()["orders"]}
    assert listed[first]["customer_kind"] == "new"
    assert listed[first]["customer_order_count"] == 1

    second = shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 1, "unit_price": 500}],
        shipping_fee=60, customer_name="Repeat", phone="01011122233", governorate="Cairo",
    )
    listed = {o["id"]: o for o in logged_in.get("/dashboard/api/shopify/orders").json()["orders"]}
    assert listed[first]["customer_kind"] == "new"
    assert listed[second]["customer_kind"] == "returning"

    returning = logged_in.get("/dashboard/api/shopify/orders?customer=returning").json()["orders"]
    assert [o["id"] for o in returning] == [second]
    new_only = logged_in.get("/dashboard/api/shopify/orders?customer=new").json()["orders"]
    assert [o["id"] for o in new_only] == [first]


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


# --------------------------------------------------------------------------
# reading Shopify's own Channel column
# --------------------------------------------------------------------------
#
# `Order.source_channel` is still what the dashboard prefers -- it is the
# record of what actually happened. This is the fallback for an order with no
# local row, and it is the shop owner's own rule for reading the admin: the
# Channel column says Online Store or the bot's app, and then the tags say
# which conversation.


def channel_of(**node):
    from integrations.shopify.admin_orders import _channel_hint

    return _channel_hint(node)


def test_an_online_store_order_is_a_website_order():
    assert channel_of(app={"name": "Online Store"}, tags=[]) == "web"


def test_a_chatbot_order_tagged_instagram_is_an_instagram_order():
    assert channel_of(
        app={"name": "Chatbot Integration"}, tags=["chatbot", "instagram", "cash-on-delivery"]
    ) == "instagram_dm"


def test_a_chatbot_order_tagged_whatsapp_is_a_whatsapp_order():
    assert channel_of(
        app={"name": "Chatbot Integration"}, tags=["chatbot", "whatsapp"]
    ) == "whatsapp"


def test_a_chatbot_order_with_no_channel_tag_is_still_not_a_website_order():
    """Every order the bot placed before the channel tag existed. Calling it a
    website sale would move the whole of the bot's history to the wrong tab."""
    assert channel_of(app={"name": "Chatbot Integration"}, tags=["chatbot"]) == "whatsapp"


def test_an_order_shopify_says_nothing_about_is_a_website_order():
    """An order this app cannot recognise is more likely a sale nobody talked
    to the bot about than a conversation that left no other trace."""
    assert channel_of(app=None, tags=[]) == "web"


def test_a_website_order_carrying_a_stray_whatsapp_tag_is_still_the_website():
    """The Channel column is read first, on purpose. Staff can tag an order in
    the admin for their own reasons, and a note about how they reached the
    customer must not relabel where the sale came from."""
    assert channel_of(app={"name": "Online Store"}, tags=["whatsapp"]) == "web"


# --------------------------------------------------------------------------
# payment method: the tag first, then the gateways
#
# Every order the bot creates goes in through `orderCreate` with nothing paid
# on it, so Shopify returns `paymentGatewayNames: []` for all of them and the
# gateway can never say how the customer is paying. The bot writes it as a tag
# instead (`shopify_orders.ORDER_TAGS`), and reading the gateway first put the
# shop's entire chatbot history in the "غير محدد" bucket.
# --------------------------------------------------------------------------


def payment_of(*, tags=(), gateways=()):
    from integrations.shopify.admin_orders import _payment_method

    return _payment_method({"tags": list(tags), "paymentGatewayNames": list(gateways)})


def test_a_cash_on_delivery_tag_is_read_even_with_no_gateway():
    """The whole of the bot's sales: tagged COD, no gateway at all. These were
    landing in `unknown`."""
    assert payment_of(tags=["chatbot", "cash-on-delivery", "whatsapp"]) == "cod"


def test_an_online_payment_tag_is_read_the_same_way():
    assert payment_of(tags=["chatbot", "online-payment"]) == "online"


def test_the_tag_beats_a_manual_gateway_left_by_marking_it_paid():
    """Collecting the cash and ticking "mark as paid" in the admin leaves the
    gateway reading `manual`, which classified a pocketful of cash as an
    online payment."""
    assert payment_of(tags=["chatbot", "cash-on-delivery"], gateways=["manual"]) == "cod"


def test_a_website_gateway_still_decides_when_there_is_no_tag():
    assert payment_of(gateways=["Cash on Delivery (COD)"]) == "cod"
    assert payment_of(gateways=["shopify_payments"]) == "online"


def test_an_order_with_neither_a_tag_nor_a_gateway_is_unknown():
    assert payment_of() == "unknown"


# --------------------------------------------------------------------------
# fulfillment orders are asked for separately
# --------------------------------------------------------------------------


def test_an_order_is_still_readable_without_the_fulfilment_scope(logged_in, bot_order, shopify):
    """`fulfillmentOrders` needs a scope `read_orders` does not imply. While it
    sat inside the order-detail query, a missing scope failed the whole
    document and the drawer showed a raw GraphQL ACCESS_DENIED instead of the
    customer, the address and the line items."""
    shopify.fulfillment_access = False
    res = logged_in.get(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["fulfillment_access"] is False
    assert body["customer_name"]
    assert body["line_items"]


def test_shipping_without_the_scope_is_refused_by_name(logged_in, bot_order, shopify):
    """Not an outage and not "already fulfilled" -- a permission that was never
    granted, named so the dashboard can say which one to add."""
    shopify.fulfillment_access = False
    res = logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/fulfill")
    assert res.json()["error"] == "fulfillment_scope_missing"


def test_a_denied_fulfillment_read_does_not_hide_a_real_outage(monkeypatch):
    """Only ACCESS_DENIED degrades. A Shopify outage still raises, because the
    two are different problems and only one of them is fixed by waiting."""
    from integrations.shopify import admin_orders
    from integrations.shopify.client import ShopifyUnavailable

    def denied(query, variables=None):
        raise ShopifyUnavailable('[{"extensions": {"code": "ACCESS_DENIED"}}]')

    def down(query, variables=None):
        raise ShopifyUnavailable("HTTP 503")

    assert admin_orders._fulfillment_orders(denied, "gid://shopify/Order/1") == ([], False)
    with pytest.raises(ShopifyUnavailable):
        admin_orders._fulfillment_orders(down, "gid://shopify/Order/1")


# --------------------------------------------------------------------------
# who counts as a new customer
# --------------------------------------------------------------------------


def test_an_order_with_nothing_identifying_the_buyer_stays_unknown():
    """A real third bucket. Calling it "new" would inflate the one number the
    answer exists to protect."""
    from integrations.shopify import admin_orders

    orders = [{"id": "gid://shopify/Order/9", "customer_gid": None, "customer_phone": None}]
    admin_orders.annotate_customer_kind(orders, frozenset({"gid://shopify/Order/1"}))
    assert orders[0]["customer_kind"] == "unknown"


def test_one_person_under_two_phone_spellings_has_one_first_order():
    """`01067177128` and `+201067177128` are one buyer, so only the earlier of
    the two orders is anybody's first."""
    from integrations.shopify import admin_orders

    assert admin_orders.identity_key(
        {"customer_gid": None, "customer_phone": "01067177128"}
    ) == admin_orders.identity_key({"customer_gid": None, "customer_phone": "+201067177128"})


def test_nothing_is_relabelled_when_shopify_would_not_answer():
    """An empty index means the walk failed, not that nobody has ordered
    before -- losing the label is a smaller failure than mislabelling every
    order in the list as new."""
    from integrations.shopify import admin_orders

    orders = [{"id": "gid://shopify/Order/9", "customer_gid": "gid://shopify/Customer/1",
               "customer_kind": "returning"}]
    admin_orders.annotate_customer_kind(orders, frozenset())
    assert orders[0]["customer_kind"] == "returning"


# --------------------------------------------------------------------------
# marking a cash-on-delivery order paid
#
# The money moves in the street, so nothing but a person can tell this shop an
# order settled -- which is why every bot order sits at PENDING until somebody
# says the courier handed it over.
# --------------------------------------------------------------------------


def test_marking_an_order_paid_moves_the_financial_status(logged_in, bot_order, shopify):
    res = logged_in.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-paid"
    )
    assert res.status_code == 200
    assert res.json()["financial_status"] == "PAID"

    detail = logged_in.get(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}").json()
    assert detail["financial_status"] == "PAID"


def test_the_local_row_is_kept_in_step(logged_in, bot_order, seeded):
    """Shopify owns the financial status, and the local `payment_status` is a
    mirror of it. Left behind, the customer's own order history would still
    say the order was unpaid after the cash was collected."""
    logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-paid")
    seeded.expire_all()
    assert seeded.get(Order, bot_order.order_id).payment_status == "paid"


def test_marking_a_website_order_paid_needs_no_local_row(logged_in, website_order):
    res = logged_in.post(f"/dashboard/api/shopify/orders/{website_order}/mark-paid")
    assert res.status_code == 200
    assert res.json()["financial_status"] == "PAID"


def test_marking_the_same_order_paid_twice_is_refused(logged_in, bot_order):
    """Shopify decides, and its refusal is passed through as a reason rather
    than swallowed -- a second click must not read as success."""
    assert logged_in.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-paid"
    ).status_code == 200
    second = logged_in.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-paid"
    )
    assert second.status_code == 409
    assert second.json()["error"] == "payment_rejected"


def test_marking_paid_requires_login(client, bot_order):
    assert client.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-paid"
    ).status_code == 401


def test_a_shopify_outage_marking_paid_is_an_outage_not_a_refusal(logged_in, bot_order, shopify):
    shopify.down = True
    res = logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-paid")
    assert res.status_code == 503
    assert res.json()["error"] == "store_unavailable"


# --------------------------------------------------------------------------
# marking an order delivered
#
# Written to Shopify as a fulfillment *event* -- the same thing a courier's
# own integration writes -- so the system has one field that means "delivered"
# rather than two that can disagree. For a cash-on-delivery order that is also
# the payment: Delivered is what settles it.
# --------------------------------------------------------------------------


def _ship(logged_in, gid):
    res = logged_in.post(f"/dashboard/api/shopify/orders/{gid}/fulfill")
    assert res.status_code == 200, res.text


def test_an_order_that_never_shipped_cannot_be_delivered(logged_in, bot_order):
    """Delivery is a fact about a parcel, and there is no parcel."""
    res = logged_in.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-delivered"
    )
    assert res.status_code == 409
    assert res.json()["error"] == "not_shipped"


def test_a_shipped_order_reports_in_transit_until_it_arrives(logged_in, bot_order):
    _ship(logged_in, bot_order.shopify_order_id)
    detail = logged_in.get(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}").json()
    assert detail["delivery_status"] == "IN_TRANSIT"


def test_delivering_a_cod_order_settles_it(logged_in, bot_order, seeded):
    """The whole point of the button: for cash on delivery, delivered *is*
    paid. Staff should never have to click two things to record one event."""
    _ship(logged_in, bot_order.shopify_order_id)
    res = logged_in.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-delivered"
    )
    assert res.status_code == 200
    assert res.json()["delivery_status"] == "DELIVERED"

    detail = logged_in.get(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}").json()
    assert detail["delivery_status"] == "DELIVERED"
    assert detail["financial_status"] == "PAID"

    seeded.expire_all()
    local = seeded.get(Order, bot_order.order_id)
    assert local.status == OrderStatus.DELIVERED.value
    assert local.payment_status == "paid"


def test_the_local_row_walks_the_stages_rather_than_jumping(logged_in, bot_order, seeded):
    """`packed_at` must not be null on an order that obviously was packed, and
    the customer's own status messages must arrive in order."""
    _ship(logged_in, bot_order.shopify_order_id)
    logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-delivered")

    seeded.expire_all()
    local = seeded.get(Order, bot_order.order_id)
    assert local.packed_at is not None
    assert local.shipped_at is not None
    assert local.delivered_at is not None


def test_delivering_a_website_order_needs_no_local_row(logged_in, website_order):
    _ship(logged_in, website_order)
    res = logged_in.post(f"/dashboard/api/shopify/orders/{website_order}/mark-delivered")
    assert res.status_code == 200
    assert "local" not in res.json()


def test_delivering_twice_is_refused(logged_in, bot_order):
    _ship(logged_in, bot_order.shopify_order_id)
    logged_in.post(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-delivered")
    second = logged_in.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-delivered"
    )
    assert second.status_code == 409
    assert second.json()["error"] == "already_delivered"


def test_marking_delivered_requires_login(client, bot_order):
    assert client.post(
        f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}/mark-delivered"
    ).status_code == 401


@pytest.mark.no_shopify
def test_a_denied_delivery_event_names_the_scope_rather_than_dumping_graphql(monkeypatch):
    """`fulfillmentEventCreate` still asks for the legacy `write_fulfillments`
    scope -- the merchant-managed/assigned pair `fulfillmentCreate` needs does
    not cover it. Staff saw the raw ACCESS_DENIED body, which named the scope
    inside a wall of JSON."""
    from integrations.shopify import admin_orders
    from integrations.shopify.client import ShopifyUnavailable

    monkeypatch.setattr(
        admin_orders, "get_order",
        lambda gid: {"cancelled": False, "fulfillment_id": "gid://shopify/Fulfillment/1",
                     "delivery_status": "IN_TRANSIT"},
    )

    def denied(query, variables=None):
        raise ShopifyUnavailable('[{"extensions": {"code": "ACCESS_DENIED"}}]')

    monkeypatch.setattr(admin_orders, "get_admin_client", lambda: denied)
    assert admin_orders.mark_delivered("gid://shopify/Order/1") == {
        "error": "delivery_scope_missing"
    }


@pytest.mark.no_shopify
def test_a_real_outage_marking_delivered_still_raises(monkeypatch):
    from integrations.shopify import admin_orders
    from integrations.shopify.client import ShopifyUnavailable

    monkeypatch.setattr(
        admin_orders, "get_order",
        lambda gid: {"cancelled": False, "fulfillment_id": "gid://shopify/Fulfillment/1",
                     "delivery_status": "IN_TRANSIT"},
    )

    def down(query, variables=None):
        raise ShopifyUnavailable("HTTP 503")

    monkeypatch.setattr(admin_orders, "get_admin_client", lambda: down)
    with pytest.raises(ShopifyUnavailable):
        admin_orders.mark_delivered("gid://shopify/Order/1")
