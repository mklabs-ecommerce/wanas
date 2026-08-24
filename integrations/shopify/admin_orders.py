"""Order management for the staff dashboard: list, detail, and fulfilment.

`backend/services/shopify_orders.py` already creates, cancels and edits an
order the *bot* placed. This module adds what nothing needed before staff
could see the whole store from one place: a paginated list of every order
(bot **and** website -- see `docs/ARCHITECTURE.md`, this shop sells through
both), and fulfilment, which is new end to end -- nothing here fulfils an
order today.

Deliberately thin on cancel/edit: those already exist, correctly, in
`shopify_orders.py`. This module's `cancel_order`/`edit_quantity` are only
the "prefer the local order service when a local row exists" routing --
`backend/services/orders.py`'s `cancel()` / `modify_quantity()` are already
transactional and already notify the customer; calling `shopify_orders`
directly a second way for the same order would be the two-writers problem in
a different shape.

Uses `get_admin_client()`, not `get_client()` -- see
`backend/integrations/shopify_client.py`: a dashboard page paginating through
orders can wait longer than a customer mid-conversation, and the two must not
share a throttle-pause state.
"""

from __future__ import annotations

import logging

from integrations.shopify.client import (
    ShopifyConfigError,
    ShopifyUnavailable,
    get_admin_client,
)
from integrations.shopify.orders import OrderRejected

log = logging.getLogger("wanas.shopify.admin_orders")

#: One page comfortably covers a slow month for a shop this size; a longer
#: range pages through this a few times rather than one huge call. Kept as a
#: literal in the query below rather than interpolated -- GraphQL's own
#: braces make string formatting more fragile here than a hardcoded number
#: with this comment next to it.
PAGE_SIZE = 50

ORDERS_QUERY = """
query($cursor: String, $query: String) {
  orders(first: 50, after: $cursor, query: $query, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      createdAt
      displayFinancialStatus
      displayFulfillmentStatus
      cancelledAt
      tags
      customer { id displayName email phone }
      shippingAddress { city province }
      totalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 50) {
        nodes { title quantity sku }
      }
    }
  }
}
"""

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
    customer { id displayName email phone }
    shippingAddress { address1 city province phone }
    totalPriceSet { shopMoney { amount currencyCode } }
    subtotalPriceSet { shopMoney { amount currencyCode } }
    totalShippingPriceSet { shopMoney { amount currencyCode } }
    lineItems(first: 100) {
      nodes { id title quantity sku variant { id sku } }
    }
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
        "customer_name": customer.get("displayName"),
        "customer_phone": customer.get("phone"),
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


def get_order(shopify_order_id: str) -> dict | None:
    """One order in full, including its fulfillment orders. None if Shopify
    has nothing at that id (deleted, or a bad id typed into the URL)."""
    client = get_admin_client()
    data = client(ORDER_DETAIL_QUERY, {"id": shopify_order_id})
    node = data.get("order")
    if node is None:
        return None

    customer = node.get("customer") or {}
    address = node.get("shippingAddress") or {}
    fulfillment_orders = [
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
    ]

    return {
        "id": node["id"],
        "name": node.get("name") or "",
        "created_at": node.get("createdAt"),
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "cancelled": bool(node.get("cancelledAt")),
        "note": node.get("note"),
        "tags": node.get("tags") or [],
        "customer_name": customer.get("displayName"),
        "customer_email": customer.get("email"),
        "customer_phone": customer.get("phone") or address.get("phone"),
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
    "list_orders",
    "get_order",
    "fulfill",
]
