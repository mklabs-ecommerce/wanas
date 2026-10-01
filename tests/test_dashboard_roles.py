"""Owner and page moderator: a moderator never receives the shop's money.

Every financial endpoint answers a moderator 403, and every mixed one
(orders, customers, the queue, a conversation) answers without a single
money field -- checked on the JSON the server sends, not on what the page
chooses to draw.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import (
    collections_api,
    customers_api,
    inbox_api,
    insights_api,
    inventory_api,
    money,
    queue_api,
    settings_api,
    shopify_api,
    staff_api,
    stats_api,
    web as dashboard,
)
from domain.models import Order, Staff
from domain.services import auth, carts, orders, staff_admin

SECRET = "test-dashboard-secret"
VARIANT = "rehla-hoodie-s-olive"
CUSTOMER = "201555000111"


@pytest.fixture()
def client(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)
    app = FastAPI()
    for module in (dashboard, shopify_api, stats_api, insights_api, settings_api, queue_api,
                   customers_api, collections_api, inventory_api, inbox_api, staff_api):
        app.include_router(module.router)
    return TestClient(app)


@pytest.fixture()
def accounts(seeded):
    staff_admin.create(seeded, "owner", "owner-password-1", role=staff_admin.OWNER_ROLE)
    staff_admin.create(seeded, "mod", "moderator-pass-1", role=staff_admin.MODERATOR_ROLE)
    seeded.commit()


def _login(client, username, password):
    res = client.post("/dashboard/api/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return client


@pytest.fixture()
def moderator(client, accounts):
    return _login(client, "mod", "moderator-pass-1")


@pytest.fixture()
def owner(client, accounts):
    return _login(client, "owner", "owner-password-1")


@pytest.fixture()
def bot_order(cairo_rate, seeded) -> Order:
    carts.add(seeded, "whatsapp", CUSTOMER, VARIANT, 1)
    result = orders.place_order(
        seeded, channel="whatsapp", external_id=CUSTOMER, customer_name="Hazem",
        governorate="Cairo", address="1 Test Street", contact_phone="01055566677",
    )
    assert "error" not in result, result
    order = seeded.get(Order, result["order_id"])
    seeded.commit()
    return order


@pytest.fixture()
def website_order(shopify) -> str:
    return shopify.seed_order(
        items=[{"variant_id": VARIANT, "quantity": 2, "unit_price": 650}],
        shipping_fee=60, customer_name="Website Customer", phone="01099988877",
        address="2 Storefront St", governorate="Giza",
    )


def _money_keys(value, *, keep_prices=False, path="") -> list[str]:
    """Every money-shaped key anywhere in a JSON answer."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if money.is_money_key(key, keep_prices=keep_prices):
                found.append(f"{path}.{key}")
            found += _money_keys(item, keep_prices=keep_prices, path=f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found += _money_keys(item, keep_prices=keep_prices, path=f"{path}[{i}]")
    return found


FINANCIAL = [
    "/dashboard/api/stats",
    "/dashboard/api/stats?days=7",
    "/dashboard/api/insights",
    "/dashboard/api/settings/flags",
    "/dashboard/api/settings/status",
    "/dashboard/api/settings/test-numbers",
    "/dashboard/api/staff",
]


@pytest.mark.parametrize("path", FINANCIAL)
def test_a_moderator_is_refused_every_financial_endpoint(moderator, path):
    res = moderator.get(path)
    assert res.status_code == 403, (path, res.status_code)
    assert res.json()["error"] == "forbidden"


def test_a_moderator_cannot_change_a_fee_or_a_setting(moderator):
    assert moderator.post("/dashboard/api/settings/flags/voice_notes", json={"enabled": False}).status_code == 403
    assert moderator.post("/dashboard/api/staff", json={"username": "x", "password": "y" * 10}).status_code == 403


def test_the_owner_still_reaches_them(owner):
    assert owner.get("/dashboard/api/stats").status_code != 403
    assert owner.get("/dashboard/api/staff").status_code == 200


def test_me_tells_the_page_whether_to_draw_money(moderator):
    me = moderator.get("/dashboard/api/me").json()
    assert me["role"] == "moderator" and me["sees_money"] is False
    assert "analytics" not in me["permissions"] and "settings" not in me["permissions"]
    assert {"inbox", "orders", "customers", "queue", "products"} <= set(me["permissions"])


def test_orders_reach_a_moderator_with_status_and_no_amounts(moderator, bot_order, website_order):
    listing = moderator.get("/dashboard/api/shopify/orders")
    assert listing.status_code == 200
    body = listing.json()
    assert body["orders"], body
    assert _money_keys(body) == []
    entry = next(o for o in body["orders"] if o["id"] == website_order)
    assert "fulfillment_status" in entry and entry["customer_name"] == "Website Customer"

    detail = moderator.get(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}")
    assert detail.status_code == 200
    assert _money_keys(detail.json()) == []
    assert detail.json()["fulfillment_orders"][0]["status"] == "OPEN"


def test_the_owner_gets_the_amounts(client, accounts, bot_order):
    _login(client, "owner", "owner-password-1")
    body = client.get(f"/dashboard/api/shopify/orders/{bot_order.shopify_order_id}").json()
    assert "total" in body and _money_keys(body)


def test_customers_reach_a_moderator_without_what_they_spent(moderator, bot_order, website_order):
    for path in ("/dashboard/api/customers", "/dashboard/api/shopify/customers"):
        res = moderator.get(path)
        assert res.status_code == 200, path
        assert _money_keys(res.json()) == [], path
    listing = moderator.get("/dashboard/api/customers").json()
    rows = listing.get("customers") or listing.get("clients") or []
    client_id = next(c["client_id"] for c in rows if c.get("client_id"))
    detail = moderator.get(f"/dashboard/api/customers/{client_id}")
    assert detail.status_code == 200
    assert _money_keys(detail.json()) == []


def test_queue_conversation_and_inbox_carry_no_amounts(moderator, bot_order):
    for path in (
        "/dashboard/api/queue",
        "/dashboard/api/conversations",
        f"/dashboard/api/conversations/whatsapp/{CUSTOMER}",
        "/dashboard/api/inbox",
    ):
        res = moderator.get(path)
        assert res.status_code == 200, (path, res.text[:200])
        assert _money_keys(res.json()) == [], path


def test_a_product_price_is_catalogue_on_a_catalogue_screen(moderator):
    res = moderator.get("/dashboard/api/shopify/inventory")
    assert res.status_code == 200
    body = res.json()
    # Unit prices stay (a moderator answering «بكام؟» needs them) ...
    assert _money_keys(body, keep_prices=True) == []
    # ... but what the stock is worth does not.
    assert "retail_value" not in json.dumps(body)


def test_the_scrubber():
    payload = {
        "orders": [{"name": "#1001", "total": "560.00", "subtotal": "500", "shipping_fee": "60",
                    "line_items": [{"title": "Hoodie", "quantity": 1, "unit_price": "500"}],
                    "fulfillment_status": "UNFULFILLED"}],
        "totals": {"count": 1, "pending": 1, "revenue": "560", "total_units": 3},
        "amount_spent": "560", "cancelled_amount": "0", "average_order_value": "560",
    }
    clean = money.scrub(payload)
    assert clean == {
        "orders": [{"name": "#1001", "line_items": [{"title": "Hoodie", "quantity": 1}],
                    "fulfillment_status": "UNFULFILLED"}],
        "totals": {"count": 1, "pending": 1, "total_units": 3},
    }
    assert money.scrub({"price": "450"}, keep_prices=True) == {"price": "450"}


def test_a_moderator_holds_its_boundary_whatever_is_stored(seeded):
    member = staff_admin.create(seeded, "m2", "moderator-pass-2", role="moderator")
    member.permissions = list(staff_admin.PERMISSION_KEYS)  # tampered with
    assert "analytics" not in staff_admin.permission_keys(member)
    assert "settings" not in staff_admin.permission_keys(member)
    assert not staff_admin.sees_money(member)


def test_manage_create_user_hashes_and_prints_once(capsys):
    from domain.db import session_scope
    from manage import main

    assert main(["create-user", "boss", "--role", "owner"]) == 0
    assert main(["create-user", "page", "--role", "moderator"]) == 0
    printed = capsys.readouterr().out
    password = printed.strip().splitlines()[-1].removeprefix("password: ")
    assert len(password) >= 20
    with session_scope() as db:
        page = db.query(Staff).filter_by(username="page").one()
        assert page.role == "moderator" and password not in page.password_hash
        assert auth.authenticate(db, "page", password) is not None
    assert main(["create-user", "page", "--role", "moderator"]) == 1  # no duplicates


def test_create_user_refuses_any_other_role():
    from manage import main

    with pytest.raises(SystemExit):
        main(["create-user", "x", "--role", "staff"])
