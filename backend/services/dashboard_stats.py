"""Store-wide KPIs for the dashboard's Statistics section.

Built on `shopify_admin_orders.list_orders`, not the local `orders` table --
see that module's docstring and `docs/ARCHITECTURE.md`: this shop sells
through the bot *and* the website, and only bot orders ever reach Postgres.
A revenue number computed from Postgres alone would silently under-report
every website sale, which is worse than a slower page load.

Conversion rate is deliberately not computed here. Shopify's Admin API
(the scope this app's token uses) does not expose site traffic or sessions,
and a bot-conversation proxy for it would answer a different question than
"conversion rate" usually means -- see the plan this module was built from.

Bounded by page count, not by trusting the caller's date range to be small:
a shop this size rarely has more than a few hundred orders in 90 days, but
nothing here should hang a dashboard page load if that assumption is ever
wrong.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.services import shopify_admin_orders

#: Hard ceiling on pages fetched for one stats request. At 50 orders/page
#: this is 5,000 orders -- past that, the page still returns, just capped,
#: rather than the request growing unboundedly slow.
MAX_PAGES = 100

#: How many best-selling lines to report.
TOP_PRODUCTS = 10


@dataclass(frozen=True)
class DateRange:
    start: datetime
    end: datetime

    def as_query(self) -> str:
        # The upper bound is the *end* of that day, not midnight at its
        # start -- a bare date would exclude every order placed after
        # 00:00:00 on the last day of the range, which is all of them.
        end_of_day = f"{self.end.date().isoformat()}T23:59:59Z"
        return f"created_at:>={self.start.date().isoformat()} AND created_at:<={end_of_day}"


def range_for_days(days: int) -> DateRange:
    end = datetime.now(UTC)
    start = end - timedelta(days=max(days, 1))
    return DateRange(start=start, end=end)


def fetch_orders_in_range(date_range: DateRange, *, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    """Every order in the range, paginated. Returns `(orders, truncated)` --
    `truncated` is True only if `max_pages` was hit, so the caller can tell
    staff the numbers are a floor, not the whole range."""
    query = date_range.as_query()
    orders: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        page = shopify_admin_orders.list_orders(query=query, cursor=cursor)
        orders.extend(page["orders"])
        if not page["has_next_page"]:
            return orders, False
        cursor = page["end_cursor"]
    return orders, True


def summarize(orders: list[dict]) -> dict:
    """The KPI cards and chart series, computed from an already-fetched page
    of orders. Kept separate from `fetch_orders_in_range` so the math is
    testable without a fake network round trip for every case."""
    active = [o for o in orders if not o["cancelled"]]
    revenue = sum((Decimal(o["total"]) for o in active), Decimal("0"))
    order_count = len(active)
    cancelled_count = len(orders) - order_count
    aov = (revenue / order_count) if order_count else Decimal("0")

    customers = {
        o.get("customer_phone") or o.get("customer_name")
        for o in active
        if o.get("customer_phone") or o.get("customer_name")
    }

    best_sellers: Counter[str] = Counter()
    for order in active:
        for line in order.get("line_items") or []:
            key = line.get("title") or line.get("sku") or "?"
            best_sellers[key] += int(line.get("quantity") or 0)

    by_day: Counter[str] = Counter()
    revenue_by_day: dict[str, Decimal] = {}
    for order in active:
        day = (order.get("created_at") or "")[:10]
        if not day:
            continue
        by_day[day] += 1
        revenue_by_day[day] = revenue_by_day.get(day, Decimal("0")) + Decimal(order["total"])

    status_breakdown: Counter[str] = Counter()
    for order in orders:
        if order["cancelled"]:
            status_breakdown["cancelled"] += 1
        else:
            status_breakdown[order.get("fulfillment_status") or "UNFULFILLED"] += 1

    source_breakdown: Counter[str] = Counter(o["source"] for o in active)

    return {
        "revenue": str(revenue),
        "order_count": order_count,
        "cancelled_count": cancelled_count,
        "customer_count": len(customers),
        "average_order_value": str(aov),
        "best_sellers": [
            {"title": title, "quantity": qty} for title, qty in best_sellers.most_common(TOP_PRODUCTS)
        ],
        "revenue_by_day": [
            {"date": day, "revenue": str(revenue_by_day[day]), "orders": by_day[day]}
            for day in sorted(revenue_by_day)
        ],
        "status_breakdown": dict(status_breakdown),
        "source_breakdown": dict(source_breakdown),
    }


def stats_for_days(days: int) -> dict:
    date_range = range_for_days(days)
    orders, truncated = fetch_orders_in_range(date_range)
    result = summarize(orders)
    result["range_days"] = days
    result["truncated"] = truncated
    return result


__all__ = ["DateRange", "range_for_days", "fetch_orders_in_range", "summarize", "stats_for_days", "MAX_PAGES"]
