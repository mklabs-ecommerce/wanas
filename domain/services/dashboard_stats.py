"""Store-wide KPIs for the dashboard's Statistics section.

Built on `shopify_admin_orders.list_orders`, not the local `orders` table --
see that module's docstring and `docs/ARCHITECTURE.md`: this shop sells
through the bot *and* the website, and only bot orders ever reach Postgres.
A revenue number computed from Postgres alone would silently under-report
every website sale, which is worse than a slower page load.

Conversion rate is still not computed here, and for the original reason:
Shopify's Admin API (the scope this app's token uses) does not expose site
traffic or sessions, so a site-wide conversion rate is not derivable from
anything this module can read. What the dashboard *does* show is a narrower,
honestly-labelled figure -- orders placed through the bot over conversations
the bot held -- assembled in the browser from this payload and the messaging
one (`dashboard/insights_api.py`), and captioned on screen as
conversation-to-order rather than as "conversion rate" unqualified. It is
built there rather than here precisely so that neither router has to reach
into the other's data source to produce it.

Bounded by page count, not by trusting the caller's date range to be small:
a shop this size rarely has more than a few hundred orders in 90 days, but
nothing here should hang a dashboard page load if that assumption is ever
wrong.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from integrations.shopify import admin_orders as shopify_admin_orders

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
        # Both bounds explicit UTC. A bare date on the lower bound (no `Z`)
        # is parsed by Shopify as midnight in the *store's* timezone, not
        # UTC -- since `start`/`end` are computed from `datetime.now(UTC)`,
        # that silently shifted the window by Cairo's UTC+2/+3 offset and
        # made it asymmetric against the explicit-UTC upper bound, dropping
        # orders placed in the first couple of hours of the range's first day.
        start_of_day = f"{self.start.date().isoformat()}T00:00:00Z"
        end_of_day = f"{self.end.date().isoformat()}T23:59:59Z"
        return f"created_at:>={start_of_day} AND created_at:<={end_of_day}"


def range_for_days(days: int) -> DateRange:
    end = datetime.now(UTC)
    start = end - timedelta(days=max(days, 1))
    return DateRange(start=start, end=end)


def range_for_dates(start: date, end: date) -> DateRange:
    """A window the caller picked, both ends inclusive.

    `as_query` above already floors to `T00:00:00Z` / `T23:59:59Z` off
    `.date()`, so a single day (`start == end`) is a whole day rather than an
    empty instant. Kept next to `range_for_days` so both ways of naming a
    window produce the same shape and go through the same query builder.
    """
    return DateRange(
        start=datetime.combine(start, datetime.min.time(), tzinfo=UTC),
        end=datetime.combine(end, datetime.min.time(), tzinfo=UTC),
    )


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


def _is_test_order(order: dict, exclude_phones: set[str]) -> bool:
    if not exclude_phones:
        return False
    digits = "".join(ch for ch in (order.get("customer_phone") or "") if ch.isdigit())
    return bool(digits) and digits in exclude_phones


#: What the Analytics page's toggles may ask for. `all` is not a value the
#: filters below ever see -- the caller drops the argument instead.
PAYMENT_FILTERS = ("all", "cod", "online", "unknown")
CHANNEL_FILTERS = ("all", "web", "whatsapp", "instagram_dm")

#: Chart bucket for an order whose shipping address carries no province or
#: city. Bucketed rather than dropped, so the governorate chart's slices add
#: up to the order count printed above it.
UNKNOWN_GOVERNORATE = ""


def summarize(
    orders: list[dict],
    *,
    exclude_phones: set[str] | None = None,
    channel_by_order_id: dict[str, str] | None = None,
    payment: str = "all",
    channel: str = "all",
) -> dict:
    """The KPI cards and chart series, computed from an already-fetched page
    of orders. Kept separate from `fetch_orders_in_range` so the math is
    testable without a fake network round trip for every case.

    `exclude_phones` (from `domain/services/test_numbers.py`) drops a staff
    number's own test purchases before anything else is computed -- not just
    from revenue, from every figure below, the same way a cancelled order
    already is excluded from all of them.

    `channel_by_order_id` maps a Shopify order id to the channel the *bot*
    recorded for it (`Order.source_channel`); anything absent from the map is
    the website. Attribution comes from Postgres, the money never does -- the
    totals are still summed from the Shopify orders passed in, so this does
    not reintroduce the under-counting a Postgres-sourced revenue number would
    (see this module's docstring).

    `payment` and `channel` narrow the whole page, not one card: the point of
    the toggles is to see what COD alone, or Instagram alone, actually looks
    like, and a page that filtered the chart but not the KPI above it would be
    worse than no toggle at all.
    """
    exclude_phones = exclude_phones or set()
    channel_by_order_id = channel_by_order_id or {}
    excluded_count = 0
    if exclude_phones:
        kept = [o for o in orders if not _is_test_order(o, exclude_phones)]
        excluded_count = len(orders) - len(kept)
        orders = kept

    for order in orders:
        order.setdefault("payment_method", "unknown")
        order["channel"] = channel_by_order_id.get(order.get("id"), "web")

    if payment != "all":
        orders = [o for o in orders if o["payment_method"] == payment]
    if channel != "all":
        orders = [o for o in orders if o["channel"] == channel]

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
    channel_breakdown: Counter[str] = Counter(o["channel"] for o in active)
    payment_breakdown: Counter[str] = Counter(o["payment_method"] for o in active)

    governorate_breakdown: Counter[str] = Counter()
    revenue_by_governorate: dict[str, Decimal] = {}
    for order in active:
        key = (order.get("governorate") or "").strip() or UNKNOWN_GOVERNORATE
        governorate_breakdown[key] += 1
        revenue_by_governorate[key] = revenue_by_governorate.get(key, Decimal("0")) + Decimal(
            order["total"]
        )

    # How many of the orders in this window came from someone who had bought
    # before. Lifetime, from Shopify's own `numberOfOrders` -- see
    # `integrations/shopify/admin_orders._customer_orders`. `unknown` is a
    # real third bucket (an order with no customer record) and is never folded
    # into "new".
    customer_kind_breakdown: Counter[str] = Counter(
        o.get("customer_kind") or "unknown" for o in active
    )

    return {
        "revenue": str(revenue),
        "order_count": order_count,
        "cancelled_count": cancelled_count,
        "customer_count": len(customers),
        "average_order_value": str(aov),
        "bot_order_count": sum(1 for o in active if o["channel"] != "web"),
        "best_sellers": [
            {"title": title, "quantity": qty} for title, qty in best_sellers.most_common(TOP_PRODUCTS)
        ],
        "revenue_by_day": [
            {"date": day, "revenue": str(revenue_by_day[day]), "orders": by_day[day]}
            for day in sorted(revenue_by_day)
        ],
        "status_breakdown": dict(status_breakdown),
        "source_breakdown": dict(source_breakdown),
        "channel_breakdown": dict(channel_breakdown),
        "payment_breakdown": dict(payment_breakdown),
        "customer_kind_breakdown": dict(customer_kind_breakdown),
        "orders_by_governorate": [
            {
                "governorate": key,
                "orders": governorate_breakdown[key],
                "revenue": str(revenue_by_governorate[key]),
            }
            for key in sorted(
                governorate_breakdown, key=lambda k: (-governorate_breakdown[k], k)
            )
        ],
        "excluded_test_orders": excluded_count,
        "filters": {"payment": payment, "channel": channel},
    }


def stats_for_range(
    date_range: DateRange,
    *,
    exclude_phones: set[str] | None = None,
    channel_by_order_id: dict[str, str] | None = None,
    payment: str = "all",
    channel: str = "all",
) -> dict:
    """The KPI payload for an already-built window. `stats_for_days` is the
    preset shorthand over this; the Analytics page's custom range comes in
    through `range_for_dates` and lands here."""
    orders, truncated = fetch_orders_in_range(date_range)
    result = summarize(
        orders,
        exclude_phones=exclude_phones,
        channel_by_order_id=channel_by_order_id,
        payment=payment,
        channel=channel,
    )
    result["truncated"] = truncated
    return result


def stats_for_days(
    days: int,
    *,
    exclude_phones: set[str] | None = None,
    channel_by_order_id: dict[str, str] | None = None,
    payment: str = "all",
    channel: str = "all",
) -> dict:
    result = stats_for_range(
        range_for_days(days),
        exclude_phones=exclude_phones,
        channel_by_order_id=channel_by_order_id,
        payment=payment,
        channel=channel,
    )
    result["range_days"] = days
    return result


__all__ = [
    "DateRange",
    "range_for_days",
    "range_for_dates",
    "fetch_orders_in_range",
    "summarize",
    "stats_for_days",
    "stats_for_range",
    "MAX_PAGES",
    "PAYMENT_FILTERS",
    "CHANNEL_FILTERS",
]
