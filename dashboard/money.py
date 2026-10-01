"""What a moderator is never sent: the shop's money.

A moderator (`staff_admin.MODERATOR_ROLE`) answers customers, works the
queue, ships orders and keeps the catalogue -- and must not see sales,
revenue, order amounts, totals, AOV, fees or anything built from them.

Two layers, both on the server:

- **Whole sections** that are nothing but money -- analytics (stats,
  insights) and settings (shipping fees) -- are not in a moderator's
  permissions, so `guard.require_permission` answers 403 before any number
  is read.
- **Mixed answers** -- an order, a customer, a queue card, a conversation's
  side panel -- keep everything a moderator needs (who, what, status,
  fulfilment) with every money field taken out. That happens here, once, in
  a route class every dashboard router is built with, rather than at forty
  call sites where the forty-first would forget. A field added tomorrow
  named `*_total` or `*_amount` is stripped without anyone remembering to.

Hiding columns in the page is a courtesy on top; the browser of a moderator
never receives the numbers to hide.

Unit prices are the one judgement call: on a product, inventory or
collection screen the price is catalogue information (a moderator answering
«بكام؟» needs it), so it is kept there; everywhere else -- order lines,
customer ledgers -- it is stripped, because price times quantity is the
order's amount by another name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import Response

from domain.db import session_scope
from domain.services import auth, staff_admin

#: Exact keys that are money wherever they appear.
MONEY_KEYS = frozenset(
    {
        "total", "subtotal", "amount", "amounts", "revenue", "sales",
        "discounts", "refunded", "fee", "fees", "aov", "average_order_value",
        "spent", "payout", "payouts", "balance", "outstanding", "has_fee",
        "revenue_by_day", "money", "retail_value", "stock_value", "inventory_value",
    }
)
#: Any key shaped like money: `total_sales`, `amount_spent`, `shipping_fee`,
#: `line_total`, `cancelled_amount`, `new_total`, `net_sales` ...
_MONEY_SHAPE = re.compile(
    r"(^|_)(total|subtotal|amount|revenue|sales|spent|fee|fees|aov|payout|payouts|"
    r"refund|refunded|discount|discounts)(_|$)"
)
#: Keys that look like money but are counts or labels. (`totals` is a
#: container, not a number: its money leaves go, its counts stay.)
_NOT_MONEY = frozenset({"total_units", "total_count", "total_orders", "total_customers"})
#: Unit prices: catalogue on a catalogue screen, an amount anywhere else.
PRICE_KEYS = frozenset(
    {
        "price", "prices", "original_price", "unit_price", "unit_original_price",
        "compare_at_price", "price_from", "price_to", "original_price_to",
        "original_price_from", "price_range",
    }
)
_PRICE_SHAPE = re.compile(r"(^|_)price(s)?(_|$)")

#: Where a unit price is catalogue information.
CATALOG_PREFIXES = (
    "/dashboard/api/shopify/products",
    "/dashboard/api/shopify/inventory",
    "/dashboard/api/shopify/collections",
    "/dashboard/api/shopify/size-charts",
    "/dashboard/api/shopify/product-types",
)


def is_money_key(key: str, *, keep_prices: bool = False) -> bool:
    lowered = key.lower()
    if lowered in _NOT_MONEY:
        return False
    if lowered in PRICE_KEYS or _PRICE_SHAPE.search(lowered):
        return not keep_prices
    return lowered in MONEY_KEYS or bool(_MONEY_SHAPE.search(lowered))


def scrub(value, *, keep_prices: bool = False):
    """`value` with every money field removed, at any depth."""
    if isinstance(value, dict):
        return {
            key: scrub(item, keep_prices=keep_prices)
            for key, item in value.items()
            if not (isinstance(key, str) and is_money_key(key, keep_prices=keep_prices))
        }
    if isinstance(value, list):
        return [scrub(item, keep_prices=keep_prices) for item in value]
    return value


def _hides_money(request: Request) -> bool:
    token = request.cookies.get("rehla_staff") or request.cookies.get("wanas_staff")
    if not token:
        return False
    with session_scope() as db:
        staff = auth.staff_from_session_token(db, token)
        return staff is not None and not staff_admin.sees_money(staff)


class MoneyGuardedRoute(APIRoute):
    """An APIRoute whose JSON answers lose their money fields on the way to a
    moderator. Every dashboard router is declared with it."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            response = await original(request)
            if "json" not in (response.media_type or response.headers.get("content-type", "")):
                return response
            if not _hides_money(request):
                return response
            try:
                payload = json.loads(response.body)
            except (ValueError, AttributeError):
                return response
            keep_prices = request.url.path.startswith(CATALOG_PREFIXES)
            scrubbed = JSONResponse(scrub(payload, keep_prices=keep_prices), status_code=response.status_code)
            for name, value in response.raw_headers:
                if name.lower() not in (b"content-length", b"content-type"):
                    scrubbed.raw_headers.append((name, value))
            return scrubbed

        return handler
