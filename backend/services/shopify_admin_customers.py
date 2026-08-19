"""Shopify's own customers, for the dashboard's Customers view.

Store-wide -- a customer who has only ever ordered on the website has no
`Client` row in wanas.db at all (see `backend/models.py`'s `Client`: it is
created at *checkout*, and only the bot's own checkout writes one). Read-only
on purpose: nothing here was asked to manage customer PII, only to show it
alongside the WhatsApp-side view (`chatbot/dashboard/customers_api.py`).
"""

from __future__ import annotations

from backend.integrations.shopify_client import (  # noqa: F401  (re-exported)
    ShopifyConfigError,
    ShopifyUnavailable,
    get_admin_client,
)

CUSTOMERS_QUERY = """
query($cursor: String, $query: String) {
  customers(first: 50, after: $cursor, query: $query) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      displayName
      email
      phone
      numberOfOrders
      amountSpent { amount currencyCode }
      defaultAddress { city province }
    }
  }
}
"""

CUSTOMER_DETAIL_QUERY = """
query($id: ID!) {
  customer(id: $id) {
    id
    displayName
    email
    phone
    numberOfOrders
    amountSpent { amount currencyCode }
    defaultAddress { address1 city province }
    orders(first: 20, sortKey: CREATED_AT, reverse: true) {
      nodes {
        id name createdAt displayFinancialStatus displayFulfillmentStatus
        totalPriceSet { shopMoney { amount } }
      }
    }
  }
}
"""


def _summary(node: dict) -> dict:
    address = node.get("defaultAddress") or {}
    return {
        "id": node["id"],
        "name": node.get("displayName"),
        "email": node.get("email"),
        "phone": node.get("phone"),
        "order_count": node.get("numberOfOrders") or 0,
        "amount_spent": ((node.get("amountSpent") or {}).get("amount")) or "0.00",
        "governorate": address.get("province") or address.get("city"),
    }


def list_customers(*, query: str | None = None, cursor: str | None = None) -> dict:
    client = get_admin_client()
    data = client(CUSTOMERS_QUERY, {"cursor": cursor, "query": query})
    block = data.get("customers") or {}
    page = block.get("pageInfo") or {}
    return {
        "customers": [_summary(n) for n in block.get("nodes") or []],
        "has_next_page": bool(page.get("hasNextPage")),
        "end_cursor": page.get("endCursor"),
    }


def get_customer(shopify_gid: str) -> dict | None:
    client = get_admin_client()
    data = client(CUSTOMER_DETAIL_QUERY, {"id": shopify_gid})
    node = data.get("customer")
    if node is None:
        return None
    out = _summary(node)
    address = node.get("defaultAddress") or {}
    out["address"] = address.get("address1")
    out["orders"] = [
        {
            "id": o["id"],
            "name": o.get("name"),
            "created_at": o.get("createdAt"),
            "financial_status": o.get("displayFinancialStatus"),
            "fulfillment_status": o.get("displayFulfillmentStatus"),
            "total": ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount") or "0.00",
        }
        for o in (node.get("orders") or {}).get("nodes") or []
    ]
    return out


__all__ = ["ShopifyConfigError", "ShopifyUnavailable", "list_customers", "get_customer"]
