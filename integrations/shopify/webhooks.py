"""Shopify telling us what happened to an order, and what staff added to the
catalogue.

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
    orders/updated              -> the total synced, when staff edited the
                                   order straight in Shopify Admin

And the second gap, the same shape: the bot's search reads wanas.db, never
the live Shopify product list, so a product staff add in Shopify Admin exists
for them and does not exist for customers. `product_import` has always closed
that -- but only at boot, and a shop that does not redeploy never boots.

    products/create             -> mirrored into wanas.db, as it happens
    products/update             -> the same, for the sizes and colours that
                                   arrive after Save

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.money import to_decimal
from config.settings import settings
from domain.db import session_scope
from domain.models import Order, OrderStatus, WebhookEvent
from domain.services import orders as order_service
from integrations.shopify import product_import

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

#: A staff edit made straight in Shopify Admin -- the only door
#: `orderEditCommit` and `check_total_against_shopify` never see. Not
#: `TOPIC_STATUS`: this never moves the status, only the money.
UPDATED_TOPIC = "orders/updated"

#: A product created or changed in Shopify Admin. The bot's search reads
#: wanas.db and never the live Shopify product list, so until one of these is
#: mirrored the product exists for staff and does not exist for customers.
#: `product_import` has always closed that gap, but only at boot, and a boot
#: is the wrong granularity for "staff added a product this afternoon".
#:
#: Both topics, not just create: Shopify fires `create` when Save is pressed,
#: while the product often still wears only the placeholder variant, which
#: `product_import` skips on purpose. The real sizes and colours arrive a
#: moment later as an update.
PRODUCT_TOPICS = {"products/create", "products/update"}

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
    """True when this delivery is ours to process, False when it is a duplicate.

    The `IntegrityError` branch is the same race
    `assistant/runtime.py::_already_processed` handles for Meta, and it is the
    one the check-then-insert above cannot close on its own: two deliveries of
    the same event can both find no row and both try to write one. Without it
    the loser raised out of the endpoint, so Shopify got a 500 for a delivery
    that was in fact handled perfectly by the winner -- and Shopify counts 5xx
    replies towards taking the subscription away, which is how tracking
    messages stop firing with nothing obviously broken.
    """
    with session_scope() as session:
        if session.get(WebhookEvent, event_id) is not None:
            return False
        session.add(WebhookEvent(platform_message_id=event_id))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return False
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
            elif topic == UPDATED_TOPIC:
                _handle_updated(session, payload)
            elif topic in PRODUCT_TOPICS:
                _handle_product(session, payload)
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


def _handle_updated(session: Session, payload: dict) -> None:
    """Money that moved because staff edited the order in Shopify Admin
    itself, never through `orderEditCommit` on this side.

    `orders/updated` also fires for a great many things this bot already
    hears about on their own topic (a fulfilment, a cancellation), so this
    only reads the total and, best-effort, the subtotal -- syncing every line
    item from a REST payload's SKUs is a lot of surface for a webhook to get
    wrong, and the number that actually reaches the customer at the door is
    the total. Fires every delivery, not just the ones that actually moved
    the number: `sync_from_admin_edit` is idempotent and only logs when
    something changed.
    """
    order = find_order(session, payload)
    if order is None:
        log.info("orders/updated for %s has no local row; nothing to sync", payload.get("name"))
        return

    try:
        total = to_decimal(payload["total_price"])
    except (KeyError, TypeError, ValueError):
        log.warning("orders/updated for %s carried no readable total", order.order_id)
        return

    subtotal = None
    try:
        subtotal = to_decimal(payload["subtotal_price"])
    except (KeyError, TypeError, ValueError):
        pass

    if order_service.sync_from_admin_edit(session, order, total=total, subtotal=subtotal):
        log.info(
            "Shopify admin edit on %s: total is now %s (subtotal %s)",
            order.order_id,
            total,
            subtotal,
        )


def _handle_product(session: Session, payload: dict) -> None:
    """Mirror a product created or changed in Shopify Admin into wanas.db.

    Delegates every rule to `product_import.import_product` -- what counts as
    a placeholder, as already known, as ours-but-lost, and as somebody else's
    SKU has to be decided in one place, or a product would be imported by this
    door and refused by the boot reconcile.

    The gid comes from `admin_graphql_api_id`, which Shopify puts on the REST
    payload precisely so a Graph API caller does not have to rebuild it from
    the numeric id.
    """
    gid = (payload.get("admin_graphql_api_id") or "").strip()
    if not gid:
        product_id = payload.get("id")
        if not product_id:
            log.warning("a product webhook arrived with no id")
            return
        gid = f"gid://shopify/Product/{product_id}"

    if (payload.get("status") or "active").lower() != "active":
        # A draft is not for sale. Importing one puts a product in the bot's
        # search that a customer can be shown and cannot be sold; the boot
        # reconcile reads ACTIVE only for the same reason, and an activation
        # arrives here as another update.
        log.info("skipping product %s: not active", gid)
        return

    entry = product_import.import_product(session, gid)
    if entry is not None:
        log.info(
            "mirrored Shopify product %r into wanas.db as %s (%d variant(s))",
            entry["title"],
            entry["product_id"],
            entry["variants"],
        )
