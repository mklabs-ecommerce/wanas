"""KPI and chart-data endpoint for the Statistics section.

Thin on purpose -- `backend/services/dashboard_stats.py` owns the math and
the Shopify pagination; this only adds the staff-cookie guard and turns a
bad `days` value into a 400 instead of an obscure traceback.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Query
from fastapi.responses import JSONResponse

from backend.db import session_scope
from backend.services import dashboard_stats, test_numbers
from backend.services.shopify_catalog import ShopifyConfigError, ShopifyUnavailable
from dashboard.guard import staff_for, unauthenticated

router = APIRouter(prefix="/dashboard/api/stats", tags=["dashboard-stats"])

#: The three presets the UI offers. Anything else is refused rather than
#: silently clamped -- a staff member reading "30 days" on screen has to be
#: looking at 30 days of data, not whatever the server decided instead.
ALLOWED_RANGES = (7, 30, 90)


@router.get("")
def stats(days: int = Query(default=30), wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    if days not in ALLOWED_RANGES:
        detail = f"days must be one of {ALLOWED_RANGES}"
        return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)

    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        exclude_phones = test_numbers.all_variants(db)
        try:
            result = dashboard_stats.stats_for_days(days, exclude_phones=exclude_phones)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return JSONResponse({"error": "store_unavailable", "detail": str(exc)}, status_code=503)
    return JSONResponse(result)
