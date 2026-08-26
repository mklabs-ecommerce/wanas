"""KPI and chart-data endpoint for the Statistics section.

Thin on purpose -- `domain/services/dashboard_stats.py` owns the math and
the Shopify pagination, `dashboard/ranges.py` owns the date window; this only
adds the staff-cookie guard and turns a bad argument into a 400 instead of an
obscure traceback.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select

from dashboard import ranges
from dashboard.guard import require_permission
from domain.db import session_scope
from domain.models import Order
from domain.services import (
    dashboard_stats,
    test_numbers,
)
from integrations.shopify.catalog import ShopifyConfigError, ShopifyUnavailable

router = APIRouter(prefix="/dashboard/api/stats", tags=["dashboard-stats"])

#: Re-exported so existing callers and tests keep the name they had. The
#: rule it stands for -- refuse an unlisted window rather than silently clamp
#: it -- now lives in `dashboard/ranges.py` alongside the custom-range parser,
#: because both analytics tabs have to agree on the same window exactly.
ALLOWED_RANGES = ranges.ALLOWED_RANGES


def _channel_map(db) -> dict[str, str]:
    """Shopify order id -> the channel the bot recorded for it.

    Attribution only. The money is still summed from Shopify's own orders in
    `dashboard_stats.summarize`; this just says which of them a customer
    placed by talking to the bot, and on which channel. Reading it from the
    `chatbot`/`whatsapp` tags instead would call every Instagram order a
    WhatsApp one, and would have nothing at all to say about the orders that
    predate the tags.
    """
    rows = db.execute(
        select(Order.shopify_order_id, Order.source_channel).where(
            Order.shopify_order_id.is_not(None)
        )
    ).all()
    return {row.shopify_order_id: row.source_channel for row in rows}


@router.get("")
def stats(
    days: int | None = Query(default=None, description="one of the presets: 7, 30, 90"),
    start: str | None = Query(default=None, description="custom range start, YYYY-MM-DD (with end)"),
    end: str | None = Query(default=None, description="custom range end, YYYY-MM-DD (inclusive)"),
    payment: str = Query(default="all", description="all | cod | online | unknown"),
    channel: str = Query(default="all", description="all | web | whatsapp | instagram_dm"),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    try:
        window = ranges.parse(days=days, start=start, end=end)
    except ranges.BadRange as exc:
        return JSONResponse({"error": "bad_arguments", "detail": str(exc)}, status_code=400)
    if payment not in dashboard_stats.PAYMENT_FILTERS:
        detail = f"payment must be one of {dashboard_stats.PAYMENT_FILTERS}"
        return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)
    if channel not in dashboard_stats.CHANNEL_FILTERS:
        detail = f"channel must be one of {dashboard_stats.CHANNEL_FILTERS}"
        return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)

    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "analytics")
        if refused is not None:
            return refused
        exclude_phones = test_numbers.all_variants(db)
        channel_by_order_id = _channel_map(db)
        try:
            result = dashboard_stats.stats_for_range(
                dashboard_stats.range_for_dates(window.start, window.end),
                exclude_phones=exclude_phones,
                channel_by_order_id=channel_by_order_id,
                payment=payment,
                channel=channel,
            )
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return JSONResponse({"error": "store_unavailable", "detail": str(exc)}, status_code=503)
    result.update(window.as_payload())
    return JSONResponse(result)
