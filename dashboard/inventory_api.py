"""The Inventory section: every variant in the store, worst first.

Products answer "what do we sell"; this answers "what is about to run out",
which no amount of browsing the product list surfaces reliably once there
are a few hundred size/colour rows. Read comes from
`integrations/shopify/admin_inventory.py`; the write goes back through
`admin_products.shopify_set_inventory`, the single place that knows the
location id and the `correction` semantics -- this router never talks to
Shopify itself.

Stock is Shopify's number, live, every time. Nothing here is cached into
Postgres: a second stock number in wanas.db is exactly the duplicate the
architecture forbids.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Cookie, Query
from fastapi.responses import JSONResponse

from dashboard.guard import staff_for, unauthenticated
from domain.db import session_scope
from integrations.shopify import admin_inventory, admin_products
from integrations.shopify.catalog import ShopifyConfigError, ShopifyUnavailable

router = APIRouter(prefix="/dashboard/api/shopify/inventory", tags=["dashboard-inventory"])

#: Rows returned in one response. The full walk still happens server-side so
#: the tiles and the low-stock filter are computed over everything, but the
#: table itself is capped -- a browser does not need 2,000 <tr> at once.
PAGE_LIMIT = 250

_FILTERS = ("all", "low", "out", "in_stock")


def _outage(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "store_unavailable", "detail": str(exc)}, status_code=503)


def _matches(row: dict, needle: str) -> bool:
    haystack = " ".join(
        str(row.get(field) or "")
        for field in ("product_title", "sku", "size", "color", "length", "category")
    )
    return needle in haystack.lower()


@router.get("")
def list_inventory(
    q: str | None = Query(default=None),
    status: str = Query(default="all"),
    low_stock_at: int = Query(default=admin_inventory.DEFAULT_LOW_STOCK, ge=0, le=999),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    if status not in _FILTERS:
        detail = f"status must be one of {_FILTERS}"
        return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)

    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

    try:
        rows, truncated = admin_inventory.inventory_rows()
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        return _outage(exc)

    # Totals are computed over the whole store, before any filter -- a tile
    # that changed when someone typed in the search box would be answering a
    # different question than the one it is labelled with.
    totals = admin_inventory.summarize(rows, low_stock_at=low_stock_at)

    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if _matches(r, needle)]
    if status == "low":
        rows = [r for r in rows if 0 < r["quantity"] <= low_stock_at]
    elif status == "out":
        rows = [r for r in rows if r["quantity"] <= 0]
    elif status == "in_stock":
        rows = [r for r in rows if r["quantity"] > 0]

    # Emptiest first: the order someone restocking actually works in.
    rows.sort(key=lambda r: (r["quantity"], r["product_title"], r["sku"]))
    shown = rows[:PAGE_LIMIT]

    return JSONResponse(
        {
            "rows": shown,
            "match_count": len(rows),
            "shown_count": len(shown),
            "totals": totals,
            "truncated": truncated,
        }
    )


@router.post("/set")
def set_quantities(
    payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    """Set the on-hand quantity for one or more variants outright.

    Absolute, not a delta: staff counting a shelf know how many are there,
    and a delta would compound every double-tap into another phantom unit.
    """
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

    updates = payload.get("updates") or []
    quantities = []
    for update in updates:
        item_id = (update or {}).get("inventory_item_id")
        quantity = (update or {}).get("quantity")
        if not item_id or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            detail = "each update needs an inventory_item_id and a non-negative integer quantity"
            return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)
        quantities.append({"inventory_item_id": item_id, "quantity": quantity})

    if not quantities:
        return JSONResponse({"error": "bad_arguments", "detail": "updates is empty"}, status_code=400)

    try:
        admin_products.shopify_set_inventory(quantities)
    except admin_products.ProductRejected as exc:
        return JSONResponse({"error": "inventory_rejected", "detail": str(exc)}, status_code=409)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        return _outage(exc)
    return JSONResponse({"ok": True, "updated": len(quantities)})
