"""Shopify telling us what happened to an order.

The gap this closes: staff fulfil and cancel orders in **Shopify Admin** --
that is the documented workflow -- and Shopify had no way to say so. The local
`orders.status` therefore never left `Confirmed`, `advance_status` was called
by nothing but the tests, and every message in
`notifications.status_change_text` (Packed / Shipped / Delivered) plus the
feedback request was code that could not run. A customer asking "طلبي فين؟"
was told `Confirmed` about a parcel that arrived yesterday.

So the shop's own actions become the trigger:

    orders/fulfilled            -> Shipped   ("طلبك في الطريق ليك 🚚")
    orders/partially_fulfilled  -> Packed
    fulfillments/update         -> Delivered, when Shopify says delivered
    orders/cancelled            -> Cancelled, stock returned locally

Three properties this has to have, because a webhook endpoint is a public URL
that writes to the order table:

* **Signed.** Every delivery is HMAC-verified against the app's webhook
  secret. With no secret configured the endpoint refuses everything rather
  than trusting whatever arrives -- an unauthenticated way to cancel orders is
  worse than no integration.
* **Idempotent.** Shopify retries for up to 48 hours and delivers at least
  once. `X-Shopify-Webhook-Id` is claimed in the same table WhatsApp uses.
* **Fast.** The status change and its WhatsApp push run after the response, so
  the 200 goes back inside Shopify's five-second budget and the event loop is
  never held by a network call.

Statuses only ever move forward, one stage at a time (`orders.advance_status`),
so a `fulfilled` webhook that arrives before anything else still produces
"packed" then "shipped" rather than skipping a stage the customer was told
about.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from domain.db import session_scope
from domain.models import Order, OrderStatus, WebhookEvent
from domain.services import orders as order_service

log = logging.getLogger("wanas.webhooks.shopify")

router = APIRouter(prefix="/webhooks/shopify", tags=["shopify"])

#: Topic -> the status the order should have reached once it has been handled.
#: `orders/cancelled` is not here: cancelling is not a forward move and runs
#: through the order service so the stock comes back.
TOPIC_STATUS = {
    "orders/partially_fulfilled": OrderStatus.PACKED.value,
    "orders/fulfilled": OrderStatus.SHIPPED.value,
}

CANCEL_TOPIC = "orders/cancelled"
FULFILMENT_TOPICS = {"fulfillments/create", "fulfillments/update"}

#: What Shopify calls a parcel that has arrived.
DELIVERED_SHIPMENT_STATUSES = {"delivered"}


def verify_signature(secret: str, raw_body: bytes, header: str | None) -> bool:
    """Shopify signs with base64(HMAC-SHA256), not hex like Meta does."""
    if not secret or not header:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), header.strip())


@router.post("")
async def inbound(request: Request, background: BackgroundTasks) -> Response:
    if not settings.shopify_webhooks_configured:
        # No secret means no way to tell Shopify from anyone else.
        return Response("shopify webhooks not configured", status_code=503)

    raw = await request.body()
    if not verify_signature(
        settings.shopify_webhook_secret, raw, request.headers.get("x-shopify-hmac-sha256")
    ):
        log.warning("rejected a Shopify webhook with a bad signature")
        return Response("bad signature", status_code=401)

    shop = (request.headers.get("x-shopify-shop-domain") or "").strip().lower()
    if settings.shopify_store_domain and shop and shop != settings.shopify_store_domain.lower():
        # A correctly signed delivery for a different store is a
        # misconfiguration, and applying it would move the wrong orders.
        log.warning("ignoring a Shopify webhook for %r", shop)
        return Response("wrong shop", status_code=403)

    topic = (request.headers.get("x-shopify-topic") or "").strip().lower()
    delivery_id = (request.headers.get("x-shopify-webhook-id") or "").strip()

    try:
        payload = await request.json()
    except Exception:
        log.warning("Shopify webhook %s had an unreadable body", topic)
        return Response("ok", status_code=200)

    # Claimed before the work is queued, for the same reason as WhatsApp: a
    # retry that lands while the first copy is still running must not send the
    # customer a second "your order shipped".
    if delivery_id and not _claim(f"shopify:{delivery_id}"):
        log.info("ignoring duplicate Shopify delivery %s (%s)", delivery_id, topic)
        return Response("ok", status_code=200)

    background.add_task(handle_topic, topic, payload)
    return Response("ok", status_code=200)


def _claim(event_id: str) -> bool:
    with session_scope() as session:
        if session.get(WebhookEvent, event_id) is not None:
            return False
        session.add(WebhookEvent(platform_message_id=event_id))
        session.flush()
    return True


# --------------------------------------------------------------------------
# handling
# --------------------------------------------------------------------------


def handle_topic(topic: str, payload: dict) -> None:
    """Apply one webhook. Never raises -- a bad payload is not worth a 500."""
    try:
        with session_scope() as session:
            if topic == CANCEL_TOPIC:
                _handle_cancelled(session, payload)
            elif topic in TOPIC_STATUS:
                _handle_status(session, payload, TOPIC_STATUS[topic])
            elif topic in FULFILMENT_TOPICS:
                _handle_fulfilment(session, payload)
            else:
                log.info("no handler for Shopify topic %r", topic)
    except Exception:
        log.exception("failed to handle Shopify webhook %s", topic)


def find_order(session: Session, payload: dict) -> Order | None:
    """The local order a Shopify payload refers to.

    Three ways, in order of how much they can be trusted: the GraphQL id we
    stored at creation, the same id rebuilt from the REST numeric id, and the
    human order name. Matching on the name alone would be enough right up
    until the shop's numbering is reset.
    """
    gid = (payload.get("admin_graphql_api_id") or "").strip()
    if gid:
        found = session.scalar(select(Order).where(Order.shopify_order_id == gid))
        if found is not None:
            return found

    numeric = payload.get("id")
    if numeric:
        rebuilt = f"gid://shopify/Order/{numeric}"
        found = session.scalar(select(Order).where(Order.shopify_order_id == rebuilt))
        if found is not None:
            return found

    name = (payload.get("name") or "").strip()
    if name:
        return session.scalar(select(Order).where(Order.shopify_order_name == name))
    return None


def advance_to(session: Session, order: Order, target: str) -> None:
    """Walk the order forward to `target`, one documented stage at a time.

    The walk itself is `order_service.advance_to` -- the dashboard's own
    "delivered" button takes the same steps, and one copy of this loop is what
    keeps the two agreeing about which pushes the customer gets. This wrapper
    is the webhook's voice: a cancelled order is a normal thing to be told
    about and worth a log line, not an error to hand back to Shopify.
    """
    if order.status == OrderStatus.CANCELLED.value:
        log.info("%s is cancelled; ignoring a move to %s", order.order_id, target)
        return
    order_service.advance_to(session, order, target)


def _handle_status(session: Session, payload: dict, target: str) -> None:
    order = find_order(session, payload)
    if order is None:
        # Orders placed in the admin or on the storefront have no local row.
        # Normal, and not something to alert on.
        log.info("Shopify order %s has no local row; nothing to update", payload.get("name"))
        return
    log.info("Shopify moved %s to %s", order.order_id, target)
    advance_to(session, order, target)


def _handle_fulfilment(session: Session, payload: dict) -> None:
    """A fulfilment update. Only the delivered signal is acted on.

    Everything before delivery is already covered by `orders/fulfilled`, and
    acting on each in-transit update would push a message per courier scan.
    """
    status = (payload.get("shipment_status") or "").strip().lower()
    if status not in DELIVERED_SHIPMENT_STATUSES:
        return

    order_payload = {
        "id": payload.get("order_id"),
        "admin_graphql_api_id": payload.get("admin_graphql_api_order_id") or "",
        "name": payload.get("name") or "",
    }
    order = find_order(session, order_payload)
    if order is None:
        log.info("delivered fulfilment for an order with no local row (%s)", payload.get("order_id"))
        return
    log.info("Shopify reported %s delivered", order.order_id)
    advance_to(session, order, OrderStatus.DELIVERED.value)


def _handle_cancelled(session: Session, payload: dict) -> None:
    order = find_order(session, payload)
    if order is None:
        log.info("cancelled Shopify order %s has no local row", payload.get("name"))
        return
    if order.status == OrderStatus.CANCELLED.value:
        return

    result = order_service.cancel(
        session,
        order,
        by="shopify",
        notify_customer=True,
        # Shopify cancelled it and restocked on its own side; asking it to do
        # that again is a call that can only fail.
        remote_already_cancelled=True,
    )
    if "error" in result:
        # A shipped order cancelled in the admin is a real situation and the
        # local rules refuse it. Worth a loud log, not a crash: staff know
        # what they did and the two sides disagreeing is what
        # `shopify_check_live` is for.
        log.warning("could not mirror a Shopify cancellation for %s: %s", order.order_id, result)
