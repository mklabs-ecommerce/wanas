"""Shopify telling us an order moved.

This closes the loop that was open: staff fulfil in Shopify Admin, and until
now nothing told this side, so `orders.status` never left `Confirmed` and every
tracking message in `notifications` was code that could not run.

The endpoint writes to the order table from a public URL, so the signature,
the shop check and the idempotency claim are as much the subject of these
tests as the status changes are.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings
from backend.models import Order, OrderStatus, ShippingRate
from backend.services import carts, notifications, orders
from backend.webhooks import shopify as hook

SECRET = "test-shopify-secret"
SHOP = "wanas-test.myshopify.com"
VARIANT = "wanas-hoodie-s-olive"
WHO = "201555444333"
SHOPIFY_NUMERIC_ID = 1234567890


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(
        hook,
        "settings",
        dataclasses.replace(settings, shopify_webhook_secret=SECRET, shopify_store_domain=SHOP),
    )


@pytest.fixture()
def client(seeded, configured):
    app = FastAPI()
    app.include_router(hook.router)
    return TestClient(app)


@pytest.fixture()
def outbox(monkeypatch):
    """Capture what the customer would be sent."""
    sender = notifications.LogSender()
    monkeypatch.setattr(notifications, "_sender", sender)
    return sender.sent


@pytest.fixture()
def placed(seeded):
    """A confirmed order, as plain values.

    Values rather than the ORM object on purpose: the webhook handler opens its
    own session, and a test session still holding a transaction is a write lock
    the handler waits on until SQLite gives up. Reading the identifiers once and
    letting go is what keeps the two sides from deadlocking over a file.
    """
    seeded.get(ShippingRate, "Cairo").fee = 60
    carts.add(seeded, "whatsapp", WHO, VARIANT, 1)
    seeded.commit()
    result = orders.place_order(
        seeded,
        channel="whatsapp",
        external_id=WHO,
        customer_name="Nour",
        governorate="Cairo",
        address="7 Test Street",
        contact_phone="01000000111",
    )
    assert "error" not in result, result
    seeded.commit()

    order = seeded.get(Order, result["order_id"])
    info = {
        "order_id": order.order_id,
        "shopify_order_id": order.shopify_order_id,
        "name": order.shopify_order_name,
    }
    seeded.rollback()
    return info


@pytest.fixture()
def post(client, seeded):
    """Send a signed delivery, after letting go of the test's own transaction."""

    def _post(topic, body, *, secret=SECRET, shop=SHOP, delivery_id="whid-1"):
        seeded.rollback()
        raw = json.dumps(body).encode()
        headers = {
            "content-type": "application/json",
            "x-shopify-topic": topic,
            "x-shopify-shop-domain": shop,
            "x-shopify-webhook-id": delivery_id,
        }
        if secret is not None:
            digest = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
            headers["x-shopify-hmac-sha256"] = base64.b64encode(digest).decode()
        return client.post("/webhooks/shopify", content=raw, headers=headers)

    return _post


def order_body(placed: dict) -> dict:
    return {
        "id": SHOPIFY_NUMERIC_ID,
        "admin_graphql_api_id": placed["shopify_order_id"],
        "name": placed["name"],
    }


def fulfilment_body(placed: dict, shipment_status: str) -> dict:
    return {
        "order_id": SHOPIFY_NUMERIC_ID,
        "admin_graphql_api_order_id": placed["shopify_order_id"],
        "shipment_status": shipment_status,
    }


def reload(seeded, placed) -> Order:
    seeded.expire_all()
    return seeded.get(Order, placed["order_id"])


# --- the front door -------------------------------------------------------


def test_an_unsigned_delivery_is_refused(post, placed, seeded):
    assert post("orders/fulfilled", order_body(placed), secret=None).status_code == 401
    assert reload(seeded, placed).status == OrderStatus.CONFIRMED.value


def test_a_delivery_signed_with_the_wrong_secret_is_refused(post, placed, seeded):
    assert post("orders/fulfilled", order_body(placed), secret="nope").status_code == 401
    assert reload(seeded, placed).status == OrderStatus.CONFIRMED.value


def test_a_delivery_for_another_shop_is_refused(post, placed, seeded):
    response = post("orders/fulfilled", order_body(placed), shop="someone-else.myshopify.com")
    assert response.status_code == 403
    assert reload(seeded, placed).status == OrderStatus.CONFIRMED.value


def test_the_endpoint_refuses_everything_when_no_secret_is_configured(seeded, monkeypatch):
    """No secret means no way to tell Shopify from anyone else."""
    monkeypatch.setattr(hook, "settings", dataclasses.replace(settings, shopify_webhook_secret=""))
    app = FastAPI()
    app.include_router(hook.router)
    assert TestClient(app).post("/webhooks/shopify", json={}).status_code == 503


def test_a_retried_delivery_is_only_applied_once(post, placed, outbox):
    """Shopify retries for 48 hours; the customer must not be told twice."""
    post("orders/fulfilled", order_body(placed), delivery_id="whid-same")
    post("orders/fulfilled", order_body(placed), delivery_id="whid-same")

    assert len([message for message in outbox if "الطريق" in message.text]) == 1


# --- what it actually does ------------------------------------------------


def test_a_fulfilment_ships_the_order_and_tells_the_customer(post, placed, seeded, outbox):
    assert post("orders/fulfilled", order_body(placed)).status_code == 200

    order = reload(seeded, placed)
    assert order.status == OrderStatus.SHIPPED.value
    # Walked through Packed rather than jumping, so the timestamps are real.
    assert order.packed_at is not None and order.shipped_at is not None
    assert any("الطريق" in message.text for message in outbox)


def test_each_stage_gets_its_own_message_in_order(post, placed, outbox):
    """One fulfilment walks two stages, and each one has to describe itself.

    The messages are only sent after the transaction commits. A callback that
    read `order.status` at that point saw the *final* status for both stages,
    so the customer got "your order is on its way" twice and was never told it
    had been packed.
    """
    post("orders/fulfilled", order_body(placed))

    templates = [message.template for message in outbox if message.template]
    assert templates == ["status_packed", "status_shipped"]
    assert "اتجهز" in outbox[0].text and "الطريق" in outbox[1].text


def test_a_partial_fulfilment_only_packs_it(post, placed, seeded):
    post("orders/partially_fulfilled", order_body(placed))
    assert reload(seeded, placed).status == OrderStatus.PACKED.value


def test_a_delivered_fulfilment_completes_the_order(post, placed, seeded, outbox):
    post("orders/fulfilled", order_body(placed), delivery_id="whid-1")
    post("fulfillments/update", fulfilment_body(placed, "delivered"), delivery_id="whid-2")

    order = reload(seeded, placed)
    assert order.status == OrderStatus.DELIVERED.value
    # Cash on delivery settles when the courier hands it over.
    assert order.payment_status == "paid"
    # And the feedback request that was written months ago finally goes out.
    assert any("تقيّم" in message.text for message in outbox)


def test_an_in_transit_update_says_nothing(post, placed, seeded, outbox):
    """A message per courier scan is how a shop teaches people to mute it."""
    post("orders/fulfilled", order_body(placed), delivery_id="whid-1")
    outbox.clear()
    post("fulfillments/update", fulfilment_body(placed, "in_transit"), delivery_id="whid-2")

    assert reload(seeded, placed).status == OrderStatus.SHIPPED.value
    assert outbox == []


def test_a_cancellation_in_the_admin_closes_the_local_order(post, placed, seeded):
    assert post("orders/cancelled", order_body(placed)).status_code == 200
    assert reload(seeded, placed).status == OrderStatus.CANCELLED.value


def test_a_cancellation_does_not_ask_shopify_to_cancel_again(post, placed, monkeypatch):
    """Shopify already cancelled and restocked. Calling it back can only fail."""
    from backend.services import shopify_orders

    called = []
    monkeypatch.setattr(shopify_orders, "cancel_order", lambda *a, **kw: called.append(a))

    post("orders/cancelled", order_body(placed))
    assert called == []


def test_an_order_we_do_not_have_is_ignored_quietly(post):
    """Orders placed on the storefront have no local row. That is normal."""
    body = {"id": 999, "admin_graphql_api_id": "gid://shopify/Order/999", "name": "#9999"}
    assert post("orders/fulfilled", body).status_code == 200


def test_an_unknown_topic_is_accepted_and_ignored(post, placed, seeded):
    assert post("orders/updated", order_body(placed)).status_code == 200
    assert reload(seeded, placed).status == OrderStatus.CONFIRMED.value


def test_a_status_never_goes_backwards(post, placed, seeded):
    post("orders/fulfilled", order_body(placed), delivery_id="whid-1")
    post("orders/partially_fulfilled", order_body(placed), delivery_id="whid-2")

    assert reload(seeded, placed).status == OrderStatus.SHIPPED.value


def test_a_cancelled_order_is_not_moved_along(post, placed, seeded):
    orders.cancel(seeded, seeded.get(Order, placed["order_id"]), by="customer")
    seeded.commit()

    post("orders/fulfilled", order_body(placed))
    assert reload(seeded, placed).status == OrderStatus.CANCELLED.value


def test_an_order_is_found_by_its_numeric_id_alone(post, placed, seeded):
    """REST payloads carry `id`; the GraphQL id we stored is rebuilt from it."""
    post("orders/fulfilled", {"id": SHOPIFY_NUMERIC_ID_FOR(placed)})
    assert reload(seeded, placed).status == OrderStatus.SHIPPED.value


def SHOPIFY_NUMERIC_ID_FOR(placed: dict) -> int:
    """The numeric half of the gid we stored at order time."""
    return int(placed["shopify_order_id"].rsplit("/", 1)[-1])
