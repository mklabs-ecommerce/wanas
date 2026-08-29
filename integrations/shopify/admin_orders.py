"""Order management for the staff dashboard: list, detail, and fulfilment.

`integrations/shopify/orders.py` already creates, cancels and edits an
order the *bot* placed. This module adds what nothing needed before staff
could see the whole store from one place: a paginated list of every order
(bot **and** website -- see `docs/ARCHITECTURE.md`, this shop sells through
both), and fulfilment, which is new end to end -- nothing here fulfils an
order today.

Deliberately thin on cancel/edit: those already exist, correctly, in
`shopify_orders.py`. This module's `cancel_order`/`edit_quantity` are only
the "prefer the local order service when a local row exists" routing --
`domain/services/orders.py`'s `cancel()` / `modify_quantity()` are already
transactional and already notify the customer; calling `shopify_orders`
directly a second way for the same order would be the two-writers problem in
a different shape.

Uses `get_admin_client()`, not `get_client()` -- see
`integrations/shopify/client.py`: a dashboard page paginating through
orders can wait longer than a customer mid-conversation, and the two must not
share a throttle-pause state.
"""

from __future__ import annotations

import logging
import threading
import time

from integrations.shopify.client import (
    ShopifyConfigError,
    ShopifyUnavailable,
    get_admin_client,
)

# `mark_as_paid` is re-exported, not defined here: it lives with the other
# order writes because the delivery path in `domain/services/orders.py`
# settles a cash-on-delivery order too, and that layer must not have to
# reach into a module named for the dashboard.
from integrations.shopify.orders import OrderRejected, mark_as_paid, normalise_phone

log = logging.getLogger("wanas.shopify.admin_orders")

#: One page comfortably covers a slow month for a shop this size; a longer
#: range pages through this a few times rather than one huge call. Kept as a
#: literal in the query below rather than interpolated -- GraphQL's own
#: braces make string formatting more fragile here than a hardcoded number
#: with this comment next to it.
PAGE_SIZE = 50

#: Exactly the fields `_order_summary` reads, in one place so that anything
#: else wanting an orders table -- `admin_customers.get_customer`, which shows
#: a customer's orders the same way the Orders screen does -- asks for the
#: same order and gets the same dict back. A second hand-written selection is
#: how two screens end up disagreeing about whether an order was cancelled.
ORDER_SUMMARY_FIELDS = """
  id
  name
  createdAt
  displayFinancialStatus
  displayFulfillmentStatus
  cancelledAt
  tags
  app { name }
  paymentGatewayNames
  customer { id displayName email phone numberOfOrders }
  shippingAddress { name phone city province }
  totalPriceSet { shopMoney { amount currencyCode } }
  lineItems(first: 50) { nodes { title quantity sku } }
"""

ORDERS_QUERY = """
query($cursor: String, $query: String) {
  orders(first: 50, after: $cursor, query: $query, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {%FIELDS%}
  }
}
""".replace("%FIELDS%", ORDER_SUMMARY_FIELDS)

ORDER_DETAIL_QUERY = """
query($id: ID!) {
  order(id: $id) {
    id
    name
    createdAt
    displayFinancialStatus
    displayFulfillmentStatus
    cancelledAt
    note
    tags
    app { name }
    paymentGatewayNames
    customer { id displayName email phone numberOfOrders }
    shippingAddress { name address1 city province phone }
    totalPriceSet { shopMoney { amount currencyCode } }
    subtotalPriceSet { shopMoney { amount currencyCode } }
    totalShippingPriceSet { shopMoney { amount currencyCode } }
    lineItems(first: 100) {
      nodes { id title quantity sku variant { id sku } }
    }
  }
}
"""

#: Asked *separately* from the order itself, and allowed to fail on its own.
#:
#: `fulfillmentOrders` needs `read_merchant_managed_fulfillment_orders` /
#: `read_assigned_fulfillment_orders`, which are not implied by `read_orders`.
#: While it sat inside ORDER_DETAIL_QUERY, an app without those scopes could
#: not open an order *at all*: Shopify answers the whole document with one
#: ACCESS_DENIED error, so the drawer showed a raw GraphQL dump instead of the
#: customer, the address and the line items -- every one of which the token is
#: perfectly entitled to read. Split out, a missing scope costs exactly the
#: button it actually blocks.
ORDER_FULFILLMENTS_QUERY = """
query($id: ID!) {
  order(id: $id) {
    fulfillmentOrders(first: 10) {
      nodes {
        id
        status
        lineItems(first: 100) {
          nodes { id remainingQuantity lineItem { id name sku } }
        }
      }
    }
  }
}
"""

FULFILLMENT_CREATE = """
mutation($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""


def _money(block: dict | None) -> str:
    return ((block or {}).get("shopMoney") or {}).get("amount") or "0.00"


#: Gateway names that mean the money has not moved yet. Shopify lets a shop
#: name its manual gateways whatever it likes, so this matches on substrings
#: of the lowercased name rather than on an exact string the merchant could
#: rename in the admin tomorrow.
COD_GATEWAY_HINTS = ("cash on delivery", "cash_on_delivery", "cod", "الدفع عند الاستلام")

PAYMENT_COD = "cod"
PAYMENT_ONLINE = "online"
PAYMENT_UNKNOWN = "unknown"
PAYMENT_METHODS = (PAYMENT_COD, PAYMENT_ONLINE, PAYMENT_UNKNOWN)


#: How the bot writes the payment method: as an order *tag*, because an order
#: created through `orderCreate` has no payment on it at all -- cash on
#: delivery means no gateway ran, and Shopify returns
#: `paymentGatewayNames: []` for every one of them. Read before the gateways,
#: not after, for two reasons: it is the only signal those orders carry (they
#: were all landing in `unknown`, i.e. "غير محدد", which is the whole of the
#: bot's sales), and where both exist the tag is the one that is right --
#: marking a COD order paid by hand once it was collected leaves the gateway
#: reading `manual`, which classified a pocketful of cash as an online payment.
PAYMENT_TAGS = {
    "cash-on-delivery": PAYMENT_COD,
    "cash_on_delivery": PAYMENT_COD,
    "cod": PAYMENT_COD,
    "online-payment": PAYMENT_ONLINE,
    "online_payment": PAYMENT_ONLINE,
    "paid-online": PAYMENT_ONLINE,
}


def _payment_method(node: dict) -> str:
    """Which of the three buckets the payment toggle offers this order belongs
    in. Classified once, here, so the orders list and the statistics page can
    never disagree about what "COD" counted.

    The tag wins, then the gateway names. An order with neither is `unknown`,
    never `online`: a draft, a manually created order, or one whose gateway
    Shopify did not return is not evidence that somebody paid a card, and
    folding it into `online` would quietly inflate exactly the number the
    toggle exists to separate.
    """
    for tag in (str(t).strip().lower() for t in (node.get("tags") or [])):
        if tag in PAYMENT_TAGS:
            return PAYMENT_TAGS[tag]
    gateways = [str(g).lower() for g in (node.get("paymentGatewayNames") or []) if g]
    if not gateways:
        return PAYMENT_UNKNOWN
    if any(hint in gateway for gateway in gateways for hint in COD_GATEWAY_HINTS):
        return PAYMENT_COD
    return PAYMENT_ONLINE


#: The admin's Channel column, as a value this app uses. "Online Store" is
#: the storefront; the bot's own orders arrive under the custom app's name
#: ("Chatbot Integration"), which is the shop owner's own rule for reading
#: that column: online store means the website, the app means the bot, and
#: then the tags say *which* conversation.
ONLINE_STORE_APPS = ("online store", "point of sale")

#: Channel tags the bot writes (`shopify_orders.CHANNEL_TAGS`), read back.
CHANNEL_BY_TAG = {"instagram": "instagram_dm", "whatsapp": "whatsapp"}


def _channel_hint(node: dict) -> str:
    """Which channel this order came in on, read from Shopify alone.

    Only a *hint*: `Order.source_channel` in Postgres is the record of what
    actually happened and the dashboard prefers it (see
    `dashboard/shopify_api.py::_order_channel`). This is what is left for an
    order with no local row -- a website sale, or a bot sale whose local row
    was never written -- and it is how the shop owner reads the admin: the
    Channel column first, then the tags.

    Defaults to `web` rather than to a bot channel. An order this app cannot
    recognise is more likely a sale nobody talked to the bot about than a
    conversation that left no other trace.
    """
    app = ((node.get("app") or {}).get("name") or "").strip().lower()
    tags = [str(t).strip().lower() for t in (node.get("tags") or [])]
    if app and app not in ONLINE_STORE_APPS:
        for tag in tags:
            if tag in CHANNEL_BY_TAG:
                return CHANNEL_BY_TAG[tag]
        # The app placed it but no tag says where from -- still not the
        # website. `whatsapp` is the bot's own default channel.
        if "chatbot" in tags or app:
            return "whatsapp"
    if "chatbot" in tags:
        return "whatsapp"
    return "web"


def _customer_orders(customer: dict) -> int | None:
    """The customer's *lifetime* order count, not their count inside whatever
    range is on screen -- Shopify's `numberOfOrders` is a shop-wide total, and
    an order from someone with a long history is a returning customer whether
    or not their earlier orders fall in this window.

    Returned over the wire as a string on some API versions and an int on
    others, so it is coerced rather than trusted. None when the order has no
    customer record at all, which is a third case the caller must not read as
    "new".
    """
    raw = customer.get("numberOfOrders")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _customer_display(customer: dict, address: dict) -> str | None:
    """Who to put in the Customer column.

    Every order the bot ever created before customers were attached to them
    (`shopify_orders._customer`) has no customer record at all, and no later
    call can give it one -- `orderUpdate` has no field for it. The name is not
    lost, though: it is on the shipping address, which is where the packing
    slip reads it from too.

    Deliberately name-only. The lifetime order count stays None for those
    orders, because an address cannot say whether this person has bought
    before, and "new" is not a thing to guess at -- see `_customer_orders`.
    """
    return customer.get("displayName") or address.get("name") or None


def _customer_kind(customer: dict) -> str:
    count = _customer_orders(customer)
    if count is None:
        return "unknown"
    return "returning" if count > 1 else "new"


def _order_summary(node: dict) -> dict:
    customer = node.get("customer") or {}
    address = node.get("shippingAddress") or {}
    return {
        "id": node["id"],
        "name": node.get("name") or "",
        "created_at": node.get("createdAt"),
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "cancelled": bool(node.get("cancelledAt")),
        "tags": node.get("tags") or [],
        #: The customer record this order is attached to, or None for the orders
        #: placed before the bot attached one. `dashboard/customer_ledger.py`
        #: keys on this *and* on the phone for exactly that reason.
        "customer_gid": customer.get("id"),
        "customer_name": _customer_display(customer, address),
        "customer_phone": customer.get("phone") or address.get("phone"),
        "customer_order_count": _customer_orders(customer),
        "customer_kind": _customer_kind(customer),
        "payment_gateways": node.get("paymentGatewayNames") or [],
        "payment_method": _payment_method(node),
        "channel_hint": _channel_hint(node),
        "app": ((node.get("app") or {}).get("name") or None),
        "governorate": address.get("province") or address.get("city"),
        "total": _money(node.get("totalPriceSet")),
        "line_items": [
            {"title": li.get("title"), "quantity": li.get("quantity"), "sku": li.get("sku")}
            for li in (node.get("lineItems") or {}).get("nodes") or []
        ],
        #: Set by the caller once it knows which orders have a matching local
        #: row -- this module has no Postgres session and does not decide it.
        "source": "chatbot" if "chatbot" in (node.get("tags") or []) else "website",
    }


#: `_order_summary` under a name another module may use. Same function --
#: `admin_customers` maps a customer's own orders with it so that table and
#: the Orders screen's table are the same table.
def order_summary(node: dict) -> dict:
    return _order_summary(node)


def list_orders(*, query: str | None = None, cursor: str | None = None) -> dict:
    """One page of orders, newest first.

    `query` is Shopify's own search syntax -- `"created_at:>=2026-01-01"`,
    `"financial_status:paid"`, etc. -- passed straight through, not
    reinterpreted here.

    Raises ShopifyUnavailable / ShopifyConfigError; the dashboard route
    decides how to show that rather than pretending the list is empty.
    """
    client = get_admin_client()
    data = client(ORDERS_QUERY, {"cursor": cursor, "query": query})
    block = data.get("orders") or {}
    nodes = block.get("nodes") or []
    page = block.get("pageInfo") or {}
    return {
        "orders": [_order_summary(n) for n in nodes],
        "has_next_page": bool(page.get("hasNextPage")),
        "end_cursor": page.get("endCursor"),
    }


#: Hard ceiling on pages walked for one filtered order list, mirroring
#: `domain/services/dashboard_stats.MAX_PAGES` and
#: `admin_customers.MAX_PAGES`. At 50 orders a page this is 1,000 orders.
MAX_PAGES = 20


def list_all_orders(*, query: str | None = None, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    """Every order matching `query`, paginated. Returns `(orders, truncated)`.

    The dashboard needs this for the three toggles Shopify's order search
    cannot express -- payment method, returning-vs-new, and the channel, which
    comes out of Postgres entirely. Applied to one page instead, "عند
    الاستلام" would silently mean "the COD orders among the last fifty
    orders" rather than "the last fifty COD orders", and the KPI strip above
    the table would total that subset while reading like a store figure. Same
    argument as `admin_customers.list_all_customers`, same shape.
    """
    orders: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        page = list_orders(query=query, cursor=cursor)
        orders.extend(page["orders"])
        if not page["has_next_page"]:
            return orders, False
        cursor = page["end_cursor"]
    return orders, True


# --------------------------------------------------------------------------
# new vs returning
# --------------------------------------------------------------------------

#: Just enough of every order in the shop to say which one came first for each
#: person: the id, when it was placed, and the two things that identify a
#: buyer. No line items, no money -- 250 orders a page instead of 50, and
#: cheap enough to ask on a list request.
ORDER_IDENTITY_QUERY = """
query($cursor: String) {
  orders(first: 250, after: $cursor, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      createdAt
      customer { id phone }
      shippingAddress { phone }
    }
  }
}
"""

#: 250 a page, so this is 25,000 orders before it gives up.
IDENTITY_MAX_PAGES = 100

#: How long a first-order index stays usable. It only decides a new/returning
#: label, so a minute of staleness costs at worst one chip on one row, and it
#: keeps the orders screen from re-walking the whole shop on every keystroke.
IDENTITY_TTL = 60.0

_identity_cache: tuple[float, frozenset[str]] | None = None
_identity_lock = threading.Lock()


def identity_key(order: dict) -> str | None:
    """Who placed this order, as one string, or None when nothing on it says.

    The Shopify customer id when there is one, otherwise the normalised phone
    -- the same two keys `dashboard/customer_ledger.py` indexes on, and for
    the same reason: every order the bot placed before it attached customers
    has no customer record, only a phone on the shipping address.
    """
    gid = order.get("customer_gid") or ((order.get("customer") or {}).get("id"))
    if gid:
        return str(gid)
    raw = (
        order.get("customer_phone")
        or (order.get("customer") or {}).get("phone")
        or (order.get("shippingAddress") or {}).get("phone")
    )
    if not raw:
        return None
    return normalise_phone(raw) or "".join(c for c in str(raw) if c.isdigit()) or None


def first_order_ids(*, max_pages: int = IDENTITY_MAX_PAGES) -> frozenset[str]:
    """The id of every order that was its buyer's *first*.

    Read from the whole shop in creation order, which is the only way to know
    it. Shopify's `numberOfOrders` cannot: it is a lifetime total, so the
    moment someone buys a second time it relabels their first order
    "returning" as well -- which is why the new-customer count kept shrinking
    on ranges that had not changed.
    """
    client = get_admin_client()
    seen: set[str] = set()
    firsts: set[str] = set()
    cursor = None
    for _ in range(max_pages):
        data = client(ORDER_IDENTITY_QUERY, {"cursor": cursor})
        block = data.get("orders") or {}
        for node in block.get("nodes") or []:
            key = identity_key(node)
            if key is None or key in seen:
                continue
            seen.add(key)
            firsts.add(node["id"])
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return frozenset(firsts)


def cached_first_order_ids() -> frozenset[str]:
    """`first_order_ids` behind a short TTL. Returns an empty set on a Shopify
    failure rather than raising: the caller is already holding the orders it
    wants to label, and losing the label is a smaller failure than losing the
    list."""
    global _identity_cache
    now = time.monotonic()
    with _identity_lock:
        if _identity_cache is not None and now - _identity_cache[0] < IDENTITY_TTL:
            return _identity_cache[1]
    try:
        firsts = first_order_ids()
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        log.warning("Could not index first orders, new/returning falls back: %s", exc)
        return frozenset()
    with _identity_lock:
        _identity_cache = (now, firsts)
    return firsts


def annotate_customer_kind(orders: list[dict], firsts: frozenset[str]) -> list[dict]:
    """Set `customer_kind` on each order from whether it was that buyer's
    first, in place.

    `unknown` stays a real third bucket -- an order with neither a customer
    record nor a phone genuinely cannot be placed, and calling it "new" would
    inflate the one number that answer exists to protect. With an empty
    `firsts` (Shopify would not answer) nothing is touched, so the per-order
    fallback from `_customer_kind` stands.
    """
    if not firsts:
        return orders
    for order in orders:
        if identity_key(order) is None:
            order["customer_kind"] = "unknown"
        else:
            order["customer_kind"] = "new" if order["id"] in firsts else "returning"
    return orders


#: What Shopify says when the access token has no fulfilment scope. Matched on
#: the code rather than the sentence, which Shopify is free to reword.
_ACCESS_DENIED = "ACCESS_DENIED"


def _fulfillment_orders(client, shopify_order_id: str) -> tuple[list[dict], bool]:
    """This order's fulfillment orders, and whether they could be read at all.

    Returns `([], False)` when the token lacks the fulfilment scopes rather
    than raising: not being allowed to *ship* from here is not a reason to
    refuse to *show* the order. Every other failure still raises, because a
    Shopify outage and a missing scope are different problems and only one of
    them is fixed by waiting.
    """
    try:
        data = client(ORDER_FULFILLMENTS_QUERY, {"id": shopify_order_id})
    except ShopifyUnavailable as exc:
        if _ACCESS_DENIED not in str(exc):
            raise
        log.warning(
            "Shopify denied fulfillmentOrders -- the app is missing "
            "read_merchant_managed_fulfillment_orders / "
            "read_assigned_fulfillment_orders. Orders stay readable; "
            "fulfilling from the dashboard will not work until the scope is added."
        )
        return [], False

    node = data.get("order") or {}
    return [
        {
            "id": fo["id"],
            "status": fo.get("status"),
            "line_items": [
                {
                    "id": li["id"],
                    "remaining_quantity": li.get("remainingQuantity"),
                    "name": (li.get("lineItem") or {}).get("name"),
                    "sku": (li.get("lineItem") or {}).get("sku"),
                }
                for li in (fo.get("lineItems") or {}).get("nodes") or []
            ],
        }
        for fo in (node.get("fulfillmentOrders") or {}).get("nodes") or []
    ], True


def get_order(shopify_order_id: str) -> dict | None:
    """One order in full. None if Shopify has nothing at that id (deleted, or
    a bad id typed into the URL).

    `fulfillment_orders` is fetched separately and may come back empty with
    `fulfillment_access` False -- see `_fulfillment_orders`.
    """
    client = get_admin_client()
    data = client(ORDER_DETAIL_QUERY, {"id": shopify_order_id})
    node = data.get("order")
    if node is None:
        return None

    customer = node.get("customer") or {}
    address = node.get("shippingAddress") or {}
    fulfillment_orders, fulfillment_access = _fulfillment_orders(client, shopify_order_id)

    return {
        "id": node["id"],
        "name": node.get("name") or "",
        "created_at": node.get("createdAt"),
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "cancelled": bool(node.get("cancelledAt")),
        "note": node.get("note"),
        "tags": node.get("tags") or [],
        #: The customer record this order is attached to, or None for the orders
        #: placed before the bot attached one. `dashboard/customer_ledger.py`
        #: keys on this *and* on the phone for exactly that reason.
        "customer_gid": customer.get("id"),
        "customer_name": _customer_display(customer, address),
        "customer_email": customer.get("email"),
        "customer_phone": customer.get("phone") or address.get("phone"),
        "customer_order_count": _customer_orders(customer),
        "customer_kind": _customer_kind(customer),
        "payment_gateways": node.get("paymentGatewayNames") or [],
        "payment_method": _payment_method(node),
        "channel_hint": _channel_hint(node),
        "app": ((node.get("app") or {}).get("name") or None),
        "address": address.get("address1"),
        "governorate": address.get("province") or address.get("city"),
        "subtotal": _money(node.get("subtotalPriceSet")),
        "shipping_fee": _money(node.get("totalShippingPriceSet")),
        "total": _money(node.get("totalPriceSet")),
        "line_items": [
            {"id": li["id"], "title": li.get("title"), "quantity": li.get("quantity"), "sku": li.get("sku")}
            for li in (node.get("lineItems") or {}).get("nodes") or []
        ],
        "fulfillment_orders": fulfillment_orders,
        #: False means the token may read the order but not its fulfillment
        #: orders, so the Ship button cannot work -- said once, here, rather
        #: than discovered as a GraphQL error at the moment somebody clicks it.
        "fulfillment_access": fulfillment_access,
    }


def fulfill(
    shopify_order_id: str,
    *,
    tracking_number: str | None = None,
    tracking_company: str | None = None,
    notify_customer: bool = False,
) -> dict:
    """Fulfil every open fulfillment order on this order.

    Nothing fulfilled anything before this existed. Re-reads the order's
    fulfillment orders immediately before committing -- the same "check again
    right before writing" the order and inventory paths already use -- so two
    staff clicking at once cannot fulfil the same order twice; the second
    finds nothing open and gets `{"error": "already_fulfilled"}` instead of a
    Shopify error that reads like a bug.

    Returns the fulfilled order's ids, or a payload with "error" set. Raises
    ShopifyUnavailable / ShopifyConfigError for the dashboard to show as an
    outage rather than a refusal.
    """
    order = get_order(shopify_order_id)
    if order is None:
        return {"error": "order_not_found"}
    if not order.get("fulfillment_access", True):
        # Not "already fulfilled" and not an outage: the app was never granted
        # the scope. Named so the dashboard can say which one to add.
        return {"error": "fulfillment_scope_missing"}

    open_orders = [fo for fo in order["fulfillment_orders"] if fo["status"] == "OPEN"]
    if not open_orders:
        return {"error": "already_fulfilled"}

    client = get_admin_client()
    tracking_info = None
    if tracking_number:
        tracking_info = {"number": tracking_number}
        if tracking_company:
            tracking_info["company"] = tracking_company

    fulfillment: dict = {
        "notifyCustomer": bool(notify_customer),
        "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fo["id"]} for fo in open_orders],
    }
    if tracking_info:
        fulfillment["trackingInfo"] = tracking_info

    data = client(FULFILLMENT_CREATE, {"fulfillment": fulfillment})
    result = data.get("fulfillmentCreate") or {}
    errors = result.get("userErrors") or []
    if errors:
        message = "; ".join(e.get("message", "") for e in errors)
        log.warning("Shopify refused to fulfil %s: %s", shopify_order_id, message)
        return {"error": "fulfillment_rejected", "detail": message}

    created = result.get("fulfillment") or {}
    return {"fulfillment_id": created.get("id"), "status": created.get("status")}


__all__ = [
    "OrderRejected",
    "ShopifyConfigError",
    "ShopifyUnavailable",
    "PAYMENT_COD",
    "PAYMENT_ONLINE",
    "PAYMENT_UNKNOWN",
    "PAYMENT_METHODS",
    "MAX_PAGES",
    "ORDER_SUMMARY_FIELDS",
    "order_summary",
    "list_orders",
    "list_all_orders",
    "identity_key",
    "first_order_ids",
    "cached_first_order_ids",
    "annotate_customer_kind",
    "get_order",
    "fulfill",
    "mark_as_paid",
]
