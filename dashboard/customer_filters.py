"""The one set of choices the Customers screen offers, for both of its tabs.

Order count, governorate and sort started on the store tab only, because they
were Shopify-side facts. They are not: a bot customer has a governorate on
their `Client` row and an order count in `orders`, so the same three questions
are answerable there, and a screen that offers them on one tab and not the
other is a screen where the filter bar changes shape when you click a tab.

Lives here rather than in either router because both need it and neither
should import the other -- `dashboard/customers_api.py` reads Postgres and
`dashboard/shopify_api.py` reads Shopify, and the point of this module is that
the *vocabulary* is the same across that line even though the data is not.
"""

from __future__ import annotations

from sqlalchemy import select

from domain.models import ShippingRate

#: How the Customers view may order the list.
CUSTOMER_SORTS = ("recent", "orders_desc", "orders_asc")

#: How `orders_count` is read.
ORDER_COUNT_OPS = ("eq", "gte")

#: What a customer with no address on file is bucketed as, so the governorate
#: dropdown can offer it instead of quietly dropping those rows.
UNKNOWN_GOVERNORATE = "__unknown__"


def governorate_options(db) -> list[dict]:
    """The shop's own governorate list, not whatever happened to load.

    Sourced from `ShippingRate` (seeded from `data/governorates.json`) so the
    dropdown is the same twenty-seven every other part of this app knows
    about. Building it from the customer rows on screen would make the filter
    change shape every time the list did, and would never offer a governorate
    the shop ships to but has not sold to yet.
    """
    rows = db.scalars(select(ShippingRate).order_by(ShippingRate.governorate)).all()
    return [{"key": row.governorate, "label_ar": row.label_ar} for row in rows]


def matches_governorate(customer: dict, wanted: str) -> bool:
    value = (customer.get("governorate") or "").strip()
    if wanted == UNKNOWN_GOVERNORATE:
        return not value
    return value.casefold() == wanted.casefold()


def apply_filters(
    customers: list[dict],
    *,
    orders_count: int | None,
    orders_op: str,
    governorate: str | None,
    sort: str,
) -> list[dict]:
    """Filter and sort a list of customer dicts, whichever side they came from.

    Both sides carry `order_count` as an int and `governorate` as a string, so
    this does not care which is which. `admin_customers._order_count` is what
    guarantees the first of those for the Shopify side -- Shopify sends the
    number as a *string*, and comparing `"1"` to `1` is what made every
    order-count filter return nothing at all.
    """
    if orders_count is not None:
        if orders_op == "gte":
            customers = [c for c in customers if (c.get("order_count") or 0) >= orders_count]
        else:
            customers = [c for c in customers if (c.get("order_count") or 0) == orders_count]
    if governorate:
        customers = [c for c in customers if matches_governorate(c, governorate)]
    if sort != "recent":
        customers = sorted(
            customers,
            key=lambda c: c.get("order_count") or 0,
            reverse=(sort == "orders_desc"),
        )
    return customers
