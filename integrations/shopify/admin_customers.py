"""Shopify's own customers, for the dashboard's Customers view.

Store-wide -- a customer who has only ever ordered on the website has no
`Client` row in wanas.db at all (see `domain/models.py`'s `Client`: it is
created at *checkout*, and only the bot's own checkout writes one). Everything
the dashboard reaches is read-only: nothing there was asked to manage customer
PII, only to show it alongside the WhatsApp-side view
(`dashboard/customers_api.py`).

The three writes at the bottom of this module are the one exception, and no
dashboard route calls them. They exist for `scripts/shopify_backfill_customers.py`,
which repairs the orders placed before the bot attached a customer to its own
orders -- an operator running a dry run and reading it, never a request.
"""

from __future__ import annotations

from integrations.shopify import admin_orders
from integrations.shopify.client import (  # noqa: F401  (re-exported)
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
      defaultAddress { address1 address2 city province zip country phone }
    }
  }
}
"""

#: The customer's orders are selected with `admin_orders.ORDER_SUMMARY_FIELDS`
#: and mapped by `admin_orders.order_summary`, so the drawer's order table is
#: the Orders screen's order table -- same columns, same cancelled flag, same
#: channel, and a row that opens the same order. It used to be a shorter
#: hand-written selection, which is how a cancelled order showed here with a
#: fulfilment chip and no sign it had been cancelled at all.
CUSTOMER_DETAIL_QUERY = """
query($id: ID!) {
  customer(id: $id) {
    id
    displayName
    email
    phone
    numberOfOrders
    amountSpent { amount currencyCode }
    defaultAddress { address1 address2 city province zip country phone }
    orders(first: 50, sortKey: CREATED_AT, reverse: true) {
      nodes {%FIELDS%}
    }
  }
}
""".replace("%FIELDS%", admin_orders.ORDER_SUMMARY_FIELDS)


def _order_count(node: dict) -> int:
    """`numberOfOrders` as a number.

    Shopify returns it as a *string* (`"1"`), and this shop's own data proves
    it. Handing that straight to the dashboard made every order-count filter
    return nothing at all: `"1" == 1` is False, so "customers with exactly one
    order" matched none of the customers with exactly one order.
    `admin_orders._customer_orders` has always coerced it; this is the same
    reading, in the one place the customers list goes through.
    """
    raw = node.get("numberOfOrders")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def format_address(address: dict | None) -> str:
    """One line a courier could actually use, out of Shopify's parts.

    The screen used to show `address1` alone, which on this shop's data is a
    street and a building number and nothing else -- no flat, no city, no
    governorate. That is not an address anybody can deliver to, and it read
    as if it were the whole one.

    Joined in delivery order, and empty parts are dropped rather than turned
    into a run of commas: an address with no `address2` must not come back as
    "15 شارع ...,, القاهرة". Returns "" when Shopify has nothing at all,
    which the caller shows as an em dash -- never a partial address dressed
    up as a complete one.
    """
    if not address:
        return ""
    parts = [
        address.get("address1"),
        address.get("address2"),
        address.get("city"),
        # `province` is the governorate on Egyptian addresses. Skipped when it
        # repeats the city, which Shopify does return for the two governorates
        # that are also cities.
        address.get("province") if address.get("province") != address.get("city") else None,
        address.get("zip"),
        address.get("country"),
    ]
    return "، ".join(p.strip() for p in parts if p and p.strip())


def _summary(node: dict) -> dict:
    address = node.get("defaultAddress") or {}
    return {
        "id": node["id"],
        "name": node.get("displayName"),
        "email": node.get("email"),
        "phone": node.get("phone"),
        "order_count": _order_count(node),
        "amount_spent": ((node.get("amountSpent") or {}).get("amount")) or "0.00",
        "governorate": address.get("province") or address.get("city"),
        # The whole thing, on the list row as well as in the drawer: finding
        # the customer in Cairo is one question, and knowing where in Cairo is
        # the one a person actually opens the row to answer.
        "address": format_address(address),
        # A second phone number lives on the address on Shopify, and it is
        # often the only one that answers -- the account phone is frequently
        # blank on a customer the checkout created.
        "address_phone": address.get("phone") or None,
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


#: Hard ceiling on pages walked for one filtered customer list, mirroring
#: `domain/services/dashboard_stats.MAX_PAGES`. At 50 rows a page this is
#: 1,000 customers -- past that the list still returns, flagged truncated,
#: rather than the request growing unboundedly slow.
MAX_PAGES = 20


def list_all_customers(*, query: str | None = None, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    """Every customer matching `query`, paginated. Returns `(customers,
    truncated)`.

    The dashboard needs this because the filters it offers -- governorate,
    an exact order count, and sorting by order count -- are things Shopify's
    customer search and its `CustomerSortKeys` cannot express. Applying them
    to the first 50 rows instead would answer a different question with a
    straight face: "the customer with the most orders" computed over one page
    is the most of *that page*, and nothing on screen would say so.
    """
    customers: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        page = list_customers(query=query, cursor=cursor)
        customers.extend(page["customers"])
        if not page["has_next_page"]:
            return customers, False
        cursor = page["end_cursor"]
    return customers, True


def get_customer(shopify_gid: str) -> dict | None:
    client = get_admin_client()
    data = client(CUSTOMER_DETAIL_QUERY, {"id": shopify_gid})
    node = data.get("customer")
    if node is None:
        return None
    out = _summary(node)
    out["orders"] = [
        admin_orders.order_summary(o) for o in (node.get("orders") or {}).get("nodes") or []
    ]
    return out


# --------------------------------------------------------------------------
# backfill: attaching a customer to an order that was placed without one
# --------------------------------------------------------------------------
#
# Every order the bot placed before `shopify_orders._customer` existed reached
# the admin with a shipping address and no customer, which the Orders list
# renders as "No customer". `orderCreate` is the only place a customer can be
# *upserted*, but `orderCustomerSet` can link an order to a customer that
# already exists -- so the repair is: find or create the person, then point the
# order at them. Both halves are write operations, which is why the script that
# drives this is dry-run by default.

CUSTOMER_SEARCH = """
query($q: String!) {
  customers(first: 5, query: $q) {
    nodes { id displayName phone email }
  }
}
"""

CUSTOMER_CREATE = """
mutation($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id displayName }
    userErrors { field message }
  }
}
"""

ORDER_CUSTOMER_SET = """
mutation($orderId: ID!, $customerId: ID!) {
  orderCustomerSet(orderId: $orderId, customerId: $customerId) {
    order { id name customer { id displayName } }
    userErrors { field message }
  }
}
"""


class CustomerWriteRefused(RuntimeError):
    """Shopify declined a customer write and said why."""


def _refused(result: dict, what: str) -> None:
    errors = result.get("userErrors") or []
    if errors:
        raise CustomerWriteRefused(
            f"{what}: " + "; ".join(e.get("message", "") for e in errors)
        )


def find_customer(*, phone: str | None = None, email: str | None = None) -> dict | None:
    """The existing customer with this phone or email, or None.

    Searched one field at a time rather than with an `OR` query: a phone hit
    and an email hit are not equally trustworthy, and taking the phone first
    matches how `orderCreate`'s `toUpsert` resolves the same person.
    """
    client = get_admin_client()
    for field, value in (("phone", phone), ("email", email)):
        if not value:
            continue
        data = client(CUSTOMER_SEARCH, {"q": f'{field}:"{value}"'})
        nodes = (data.get("customers") or {}).get("nodes") or []
        if nodes:
            return nodes[0]
    return None


def create_customer(
    *, first_name: str, last_name: str, phone: str | None, email: str | None
) -> dict:
    """Create a customer. Raises `CustomerWriteRefused` if Shopify declined.

    Callers must have searched first -- this does not check, and Shopify will
    refuse a duplicate phone rather than silently merging.
    """
    payload: dict[str, str] = {}
    if first_name:
        payload["firstName"] = first_name
    if last_name:
        payload["lastName"] = last_name
    if phone:
        payload["phone"] = phone
    if email:
        payload["email"] = email

    client = get_admin_client()
    data = client(CUSTOMER_CREATE, {"input": payload})
    result = data.get("customerCreate") or {}
    _refused(result, "customerCreate")
    customer = result.get("customer") or {}
    if not customer.get("id"):
        raise ShopifyUnavailable("Shopify returned no customer")
    return customer


def set_order_customer(order_gid: str, customer_gid: str) -> dict:
    """Link an existing order to an existing customer."""
    client = get_admin_client()
    data = client(ORDER_CUSTOMER_SET, {"orderId": order_gid, "customerId": customer_gid})
    result = data.get("orderCustomerSet") or {}
    _refused(result, "orderCustomerSet")
    return result.get("order") or {}


__all__ = [
    "ShopifyConfigError",
    "ShopifyUnavailable",
    "MAX_PAGES",
    "list_customers",
    "list_all_customers",
    "get_customer",
    "CustomerWriteRefused",
    "find_customer",
    "create_customer",
    "set_order_customer",
]
