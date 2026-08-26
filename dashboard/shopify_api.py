"""The Shopify/Website management section: orders today, products and
customers in the phases after it.

Sits next to `web.py` rather than growing it -- see that module's docstring.
Every route uses the same staff-cookie guard (`dashboard.guard`).

Order actions route through the local order service
(`domain/services/orders.py`) whenever a matching local `Order` row exists
-- that path is already transactional and already notifies the customer.
Only for an order with no local row (placed on the website, never touched by
the bot) does this module call `shopify_admin_orders` / `shopify_orders`
directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Cookie, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select

from dashboard.guard import require_permission
from domain.db import session_scope
from domain.models import Order, Product, ShippingRate, Variant
from domain.services import orders as orders_service
from integrations.shopify import (
    admin_customers as shopify_admin_customers,
    admin_orders as shopify_admin_orders,
    admin_products as shopify_admin_products,
    orders as shopify_orders,
)
from integrations.shopify.catalog import ShopifyConfigError, ShopifyUnavailable

router = APIRouter(prefix="/dashboard/api/shopify", tags=["dashboard-shopify"])


def _outage(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "store_unavailable", "detail": str(exc)}, status_code=503)


def _local_order_for(db, shopify_order_id: str) -> Order | None:
    return db.scalar(select(Order).where(Order.shopify_order_id == shopify_order_id))


def _local_orders_for(db, shopify_order_ids: list[str]) -> dict[str, Order]:
    if not shopify_order_ids:
        return {}
    rows = db.scalars(select(Order).where(Order.shopify_order_id.in_(shopify_order_ids))).all()
    return {row.shopify_order_id: row for row in rows}


def _local_ref(order: Order | None) -> dict | None:
    if order is None:
        return None
    return {"order_id": order.order_id, "channel": order.source_channel}


# --------------------------------------------------------------------------
# orders: list / detail
# --------------------------------------------------------------------------


#: What the Orders view's toggles may ask for. Anything else is a 400 rather
#: than a silent "all", for the same reason `stats_api.ALLOWED_RANGES` refuses
#: an unknown range: a screen that says "COD only" has to be COD only.
PAYMENT_FILTERS = ("all", *shopify_admin_orders.PAYMENT_METHODS)
CUSTOMER_FILTERS = ("all", "new", "returning")
CHANNEL_FILTERS = ("all", "web", "whatsapp", "instagram_dm")


def _bad(detail: str) -> JSONResponse:
    return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)


def _order_channel(local: Order | None) -> str:
    """Which channel an order came in on.

    Read off the local `Order` row, never off the Shopify tags. The bot tags
    every order it creates `whatsapp` regardless of the channel it was
    actually placed on, and no tag at all can be added retroactively to the
    orders already in the shop -- whereas `Order.source_channel` has recorded
    the real channel since the column existed. No local row means nobody
    talked to the bot about it, which is the website.
    """
    return local.source_channel if local is not None else "web"


@router.get("/orders")
def list_orders(
    q: str | None = Query(default=None, description="Shopify search syntax, e.g. financial_status:paid"),
    payment: str = Query(default="all", description="all | cod | online | unknown"),
    customer: str = Query(default="all", description="all | new | returning"),
    channel: str = Query(default="all", description="all | web | whatsapp | instagram_dm"),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    if payment not in PAYMENT_FILTERS:
        return _bad(f"payment must be one of {PAYMENT_FILTERS}")
    if customer not in CUSTOMER_FILTERS:
        return _bad(f"customer must be one of {CUSTOMER_FILTERS}")
    if channel not in CHANNEL_FILTERS:
        return _bad(f"channel must be one of {CHANNEL_FILTERS}")

    # None of the three is expressible in Shopify's order search: two are
    # classified from fields the query syntax has no operator for, and
    # `channel` lives in Postgres entirely. So the moment one is set, the
    # whole matching list is walked rather than one page -- filtering page one
    # would make "عند الاستلام" mean "the COD orders among the last fifty
    # orders", and the KPI strip above the table would total that subset while
    # reading like a store figure. See `admin_orders.list_all_orders`.
    filtering = payment != "all" or customer != "all" or channel != "all"

    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "orders")
        if refused is not None:
            return refused
        try:
            if filtering:
                orders, truncated = shopify_admin_orders.list_all_orders(query=q)
                end_cursor = None
            else:
                page = shopify_admin_orders.list_orders(query=q)
                orders, truncated = page["orders"], page["has_next_page"]
                end_cursor = page["end_cursor"]
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)

        local_by_id = _local_orders_for(db, [item["id"] for item in orders])
        for item in orders:
            local = local_by_id.get(item["id"])
            item["local"] = _local_ref(local)
            item["channel"] = _order_channel(local)

    if payment != "all":
        orders = [o for o in orders if o["payment_method"] == payment]
    if customer != "all":
        orders = [o for o in orders if o["customer_kind"] == customer]
    if channel != "all":
        orders = [o for o in orders if o["channel"] == channel]

    return JSONResponse(
        {
            "orders": orders,
            # `has_next_page` is meaningless once a filter ran -- the list is
            # the whole match, not a page of it. `truncated` is the honest
            # replacement: True means the page cap was hit and these numbers
            # are a floor.
            "has_next_page": (not filtering) and truncated,
            "end_cursor": end_cursor,
            "truncated": truncated,
            "filters": {"payment": payment, "customer": customer, "channel": channel},
        }
    )


@router.get("/orders/{order_gid:path}")
def order_detail(order_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "orders")
        if refused is not None:
            return refused
        try:
            order = shopify_admin_orders.get_order(order_gid)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
        if order is None:
            return JSONResponse({"error": "order_not_found"}, status_code=404)
        local = _local_order_for(db, order_gid)
        order["local"] = _local_ref(local)
        order["channel"] = _order_channel(local)
    return JSONResponse(order)


# --------------------------------------------------------------------------
# orders: actions
# --------------------------------------------------------------------------


@router.post("/orders/{order_gid:path}/fulfill")
def fulfill_order(
    order_gid: str, payload: dict = Body(default={}), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "orders")
        if refused is not None:
            return refused
        try:
            result = shopify_admin_orders.fulfill(
                order_gid,
                tracking_number=(payload.get("tracking_number") or None),
                tracking_company=(payload.get("tracking_company") or None),
                notify_customer=bool(payload.get("notify_customer", False)),
            )
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)

    if "error" in result:
        status = 404 if result["error"] == "order_not_found" else 409
        return JSONResponse(result, status_code=status)
    return JSONResponse(result)


@router.post("/orders/{order_gid:path}/cancel")
def cancel_order(order_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        staff, refused = require_permission(db, wanas_staff, "orders")
        if refused is not None:
            return refused

        local = _local_order_for(db, order_gid)
        if local is not None:
            # The same path a customer's own cancel request takes --
            # transactional, restocks, notifies. `by="staff"` is recorded in
            # the order's own modification log.
            result = orders_service.cancel(db, local, by="staff", notify_customer=True)
            status = 200 if "error" not in result else 409
            return JSONResponse(result, status_code=status)

        # A website order with no local row: nothing here to keep in step,
        # so call Shopify directly.
        try:
            shopify_orders.cancel_order(order_gid, reason="OTHER")
        except shopify_orders.OrderRejected as exc:
            return JSONResponse({"error": "cancel_rejected", "detail": str(exc)}, status_code=409)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------
# products: list / detail
# --------------------------------------------------------------------------


def _local_product_id_for_skus(db, skus: list[str]) -> str | None:
    if not skus:
        return None
    return db.scalar(select(Variant.product_id).where(Variant.variant_id.in_(skus)))


@router.get("/products")
def list_products(
    q: str | None = Query(default=None), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "products")
        if refused is not None:
            return refused
        try:
            result = shopify_admin_products.list_products(query=q)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
    return JSONResponse(result)


@router.get("/products/{product_gid:path}")
def product_detail(product_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "products")
        if refused is not None:
            return refused
        try:
            product = shopify_admin_products.get_product(product_gid)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
        if product is None:
            return JSONResponse({"error": "product_not_found"}, status_code=404)

        skus = [v["sku"] for v in product["variants"] if v.get("sku")]
        local_id = _local_product_id_for_skus(db, skus)
        product["local_product_id"] = local_id
        if local_id is not None:
            local = db.get(Product, local_id)
            product["local"] = {
                "department": local.department,
                "style": local.style,
                "collection": local.collection,
                "size_chart": local.size_chart,
            }
        else:
            # A Shopify product with no matching wanas.db row -- created
            # outside this dashboard, or never run through
            # scripts/shopify_sync.py. Editable on Shopify's own fields
            # still, but there is nowhere local to write
            # category/department/style/collection/size_chart, so the route
            # below refuses an edit here rather than inventing one.
            product["local"] = None
    return JSONResponse(product)


# --------------------------------------------------------------------------
# products: create / edit
# --------------------------------------------------------------------------

_REQUIRED_PRODUCT_FIELDS = ("title", "category", "department", "variants")


@router.post("/products")
def create_product(payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "products")
        if refused is not None:
            return refused

        missing = [f for f in _REQUIRED_PRODUCT_FIELDS if not payload.get(f)]
        if missing:
            detail = f"missing required field(s): {', '.join(missing)}"
            return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)

        try:
            result = shopify_admin_products.create_product(
                db,
                title=payload["title"],
                description=payload.get("description") or "",
                category=payload["category"],
                department=payload["department"],
                style=payload.get("style"),
                collection=payload.get("collection"),
                size_chart=payload.get("size_chart"),
                variants=payload["variants"],
                image_url=payload.get("image_url") or None,
            )
        except shopify_admin_products.ProductRejected as exc:
            return JSONResponse({"error": "product_rejected", "detail": str(exc)}, status_code=409)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
    return JSONResponse(result, status_code=201)


@router.post("/products/{local_product_id}/update")
def update_product(
    local_product_id: str, payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "products")
        if refused is not None:
            return refused
        try:
            result = shopify_admin_products.update_product(
                db,
                local_product_id,
                title=payload.get("title"),
                description=payload.get("description"),
                category=payload.get("category"),
                department=payload.get("department"),
                style=payload.get("style"),
                collection=payload.get("collection"),
                size_chart=payload.get("size_chart"),
                variant_updates=payload.get("variant_updates"),
            )
        except shopify_admin_products.ProductRejected as exc:
            return JSONResponse({"error": "product_rejected", "detail": str(exc)}, status_code=409)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)

    if result.get("error") == "product_not_found":
        return JSONResponse(result, status_code=404)
    return JSONResponse(result)


@router.post("/orders/{order_gid:path}/quantity")
def edit_quantity(
    order_gid: str, payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "orders")
        if refused is not None:
            return refused

        variant_id = (payload.get("variant_id") or "").strip()
        quantity = payload.get("quantity")
        if not variant_id or not isinstance(quantity, int) or quantity < 0:
            detail = "variant_id and a non-negative integer quantity are required"
            return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)

        local = _local_order_for(db, order_gid)
        if local is not None:
            result = orders_service.modify_quantity(db, local, variant_id, quantity)
            status = 200 if "error" not in result else 409
            return JSONResponse(result, status_code=status)

        try:
            shopify_orders.set_line_quantity(order_gid, variant_id, quantity)
        except shopify_orders.OrderRejected as exc:
            return JSONResponse({"error": "edit_rejected", "detail": str(exc)}, status_code=409)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------
# customers: Shopify's own, store-wide -- read-only. See
# dashboard/customers_api.py for the separate WhatsApp-side view.
# --------------------------------------------------------------------------


#: How the Customers view may order the list.
CUSTOMER_SORTS = ("recent", "orders_desc", "orders_asc")

#: What a customer with no address on file is bucketed as, so the governorate
#: dropdown can offer it instead of quietly dropping those rows.
UNKNOWN_GOVERNORATE = "__unknown__"


def _governorate_options(db) -> list[dict]:
    """The shop's own governorate list, not whatever happened to load.

    Sourced from `ShippingRate` (seeded from `data/governorates.json`) so the
    dropdown is the same twenty-seven every other part of this app knows
    about. Building it from the customer rows on screen would make the filter
    change shape every time the list did, and would never offer a governorate
    the shop ships to but has not sold to yet.
    """
    rows = db.scalars(select(ShippingRate).order_by(ShippingRate.governorate)).all()
    return [{"key": row.governorate, "label_ar": row.label_ar} for row in rows]


def _matches_governorate(customer: dict, wanted: str) -> bool:
    value = (customer.get("governorate") or "").strip()
    if wanted == UNKNOWN_GOVERNORATE:
        return not value
    return value.casefold() == wanted.casefold()


@router.get("/customers")
def list_customers(
    q: str | None = Query(default=None),
    orders_count: int | None = Query(default=None, ge=0, description="filter by lifetime order count"),
    orders_op: str = Query(default="eq", description="eq | gte -- how to read orders_count"),
    governorate: str | None = Query(default=None),
    sort: str = Query(default="recent", description="recent | orders_desc | orders_asc"),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    if orders_op not in ("eq", "gte"):
        return _bad("orders_op must be 'eq' or 'gte'")
    if sort not in CUSTOMER_SORTS:
        return _bad(f"sort must be one of {CUSTOMER_SORTS}")

    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "customers")
        if refused is not None:
            return refused
        options = _governorate_options(db)
        try:
            # One page is enough only when nothing is being filtered or
            # re-sorted across the whole list; the moment it is, page one is
            # the wrong denominator -- see `list_all_customers`.
            filtering = orders_count is not None or governorate or sort != "recent"
            if filtering:
                customers, truncated = shopify_admin_customers.list_all_customers(query=q)
            else:
                page = shopify_admin_customers.list_customers(query=q)
                customers, truncated = page["customers"], page["has_next_page"]
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)

    if orders_count is not None:
        if orders_op == "gte":
            customers = [c for c in customers if (c.get("order_count") or 0) >= orders_count]
        else:
            customers = [c for c in customers if (c.get("order_count") or 0) == orders_count]
    if governorate:
        customers = [c for c in customers if _matches_governorate(c, governorate)]
    if sort != "recent":
        customers = sorted(
            customers,
            key=lambda c: c.get("order_count") or 0,
            reverse=(sort == "orders_desc"),
        )

    return JSONResponse(
        {
            "customers": customers,
            # `has_next_page` is gone once anything was filtered -- the list
            # below is the whole match, not a page of it. `truncated` is the
            # honest replacement: True means the cap was hit and these numbers
            # are a floor.
            "truncated": truncated,
            "governorates": options,
            "filters": {
                "orders_count": orders_count,
                "orders_op": orders_op,
                "governorate": governorate,
                "sort": sort,
            },
        }
    )


@router.get("/customers/{customer_gid:path}")
def customer_detail(customer_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "customers")
        if refused is not None:
            return refused
        try:
            customer = shopify_admin_customers.get_customer(customer_gid)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
        if customer is None:
            return JSONResponse({"error": "customer_not_found"}, status_code=404)
    return JSONResponse(customer)
