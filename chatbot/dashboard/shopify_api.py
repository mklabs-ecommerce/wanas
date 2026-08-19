"""The Shopify/Website management section: orders today, products and
customers in the phases after it.

Sits next to `web.py` rather than growing it -- see that module's docstring.
Every route uses the same staff-cookie guard (`chatbot.dashboard.guard`).

Order actions route through the local order service
(`backend/services/orders.py`) whenever a matching local `Order` row exists
-- that path is already transactional and already notifies the customer.
Only for an order with no local row (placed on the website, never touched by
the bot) does this module call `shopify_admin_orders` / `shopify_orders`
directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Cookie, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select

from backend.db import session_scope
from backend.models import Order, Product, Variant
from backend.services import orders as orders_service
from backend.services import (
    shopify_admin_customers,
    shopify_admin_orders,
    shopify_admin_products,
    shopify_orders,
)
from backend.services.shopify_catalog import ShopifyConfigError, ShopifyUnavailable
from chatbot.dashboard.guard import staff_for, unauthenticated

router = APIRouter(prefix="/dashboard/api/shopify", tags=["dashboard-shopify"])


def _outage(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "store_unavailable", "detail": str(exc)}, status_code=503)


def _local_order_for(db, shopify_order_id: str) -> Order | None:
    return db.scalar(select(Order).where(Order.shopify_order_id == shopify_order_id))


def _local_ref(order: Order | None) -> dict | None:
    if order is None:
        return None
    return {"order_id": order.order_id, "channel": order.source_channel}


# --------------------------------------------------------------------------
# orders: list / detail
# --------------------------------------------------------------------------


@router.get("/orders")
def list_orders(
    q: str | None = Query(default=None, description="Shopify search syntax, e.g. financial_status:paid"),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        try:
            result = shopify_admin_orders.list_orders(query=q)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)

        for item in result["orders"]:
            item["local"] = _local_ref(_local_order_for(db, item["id"]))
    return JSONResponse(result)


@router.get("/orders/{order_gid:path}")
def order_detail(order_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        try:
            order = shopify_admin_orders.get_order(order_gid)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
        if order is None:
            return JSONResponse({"error": "order_not_found"}, status_code=404)
        order["local"] = _local_ref(_local_order_for(db, order_gid))
    return JSONResponse(order)


# --------------------------------------------------------------------------
# orders: actions
# --------------------------------------------------------------------------


@router.post("/orders/{order_gid:path}/fulfill")
def fulfill_order(
    order_gid: str, payload: dict = Body(default={}), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
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
        staff = staff_for(db, wanas_staff)
        if staff is None:
            return unauthenticated()

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
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        try:
            result = shopify_admin_products.list_products(query=q)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
    return JSONResponse(result)


@router.get("/products/{product_gid:path}")
def product_detail(product_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
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
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

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
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
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
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

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
# chatbot/dashboard/customers_api.py for the separate WhatsApp-side view.
# --------------------------------------------------------------------------


@router.get("/customers")
def list_customers(
    q: str | None = Query(default=None), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        try:
            result = shopify_admin_customers.list_customers(query=q)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
    return JSONResponse(result)


@router.get("/customers/{customer_gid:path}")
def customer_detail(customer_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        try:
            customer = shopify_admin_customers.get_customer(customer_gid)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
        if customer is None:
            return JSONResponse({"error": "customer_not_found"}, status_code=404)
    return JSONResponse(customer)
