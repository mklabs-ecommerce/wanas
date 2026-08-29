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

import base64
import binascii
import re

from fastapi import APIRouter, Body, Cookie, Query
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select

from dashboard import customer_filters, customer_ledger
from dashboard.guard import require_permission
from domain.db import session_scope
from domain.models import Client, Order, OrderStatus, Product, Variant
from domain.services import orders as orders_service, size_charts as size_charts_service
from integrations.shopify import (
    admin_customers as shopify_admin_customers,
    admin_orders as shopify_admin_orders,
    admin_products as shopify_admin_products,
    files as shopify_files,
    orders as shopify_orders,
)
from integrations.shopify.catalog import ShopifyConfigError, ShopifyUnavailable
from integrations.shopify.client import get_admin_client

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


def _order_channel(local: Order | None, order: dict | None = None) -> str:
    """Which channel an order came in on.

    `Order.source_channel` first: it is what actually happened, recorded by
    the process that handled the conversation, and it is right even for the
    orders the bot mistagged `whatsapp` while selling on Instagram (see
    `shopify_orders.CHANNEL_TAGS`).

    Shopify's own answer is the fallback, for an order with no local row --
    read the way the shop owner reads the admin: the Channel column says
    Online Store or the chatbot app, and then the tags say which conversation.
    That used to be a flat "web", which quietly relabelled every bot sale
    whose local row predates `shopify_order_id` as a website sale.
    """
    if local is not None:
        return local.source_channel
    return (order or {}).get("channel_hint") or "web"


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
            item["channel"] = _order_channel(local, item)
        # New/returning is a fact about the *order*, not about the customer
        # today: it says whether this was their first. Shopify's
        # `numberOfOrders` cannot answer that -- see
        # `admin_orders.first_order_ids`.
        shopify_admin_orders.annotate_customer_kind(
            orders, shopify_admin_orders.cached_first_order_ids()
        )

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
        order["channel"] = _order_channel(local, order)
        shopify_admin_orders.annotate_customer_kind(
            [order], shopify_admin_orders.cached_first_order_ids()
        )
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


@router.post("/orders/{order_gid:path}/mark-delivered")
def mark_order_delivered(order_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """Record that the parcel arrived.

    Shopify first, as a fulfillment event -- the same thing a courier's own
    integration writes, so there is one field in the system that means
    "delivered" rather than two that can disagree. Then the local row is
    walked forward to Delivered here as well, rather than left to the
    `fulfillments/update` webhook the event will also fire: the webhook is the
    right mechanism but it is not a guarantee (it needs
    `SHOPIFY_WEBHOOK_SECRET` and a reachable URL), and a staff member who
    clicked "delivered" must not have to wonder. Both paths run through
    `orders_service.advance_to`, which is idempotent -- whichever arrives
    second finds nothing left to do.

    A cash-on-delivery order settles as a *consequence*: Delivered is what
    sets `payment_status` and tells Shopify (see
    `orders_service.advance_status`). An order already paid online is simply
    told again and Shopify declines, which costs a log line and nothing else.
    """
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "orders")
        if refused is not None:
            return refused
        try:
            result = shopify_admin_orders.mark_delivered(order_gid)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
        if "error" in result:
            status = 404 if result["error"] == "order_not_found" else 409
            return JSONResponse(result, status_code=status)

        local = _local_order_for(db, order_gid)
        if local is not None:
            local_result = orders_service.advance_to(db, local, OrderStatus.DELIVERED.value)
            result["local"] = local_result
    return JSONResponse(result)


@router.post("/orders/{order_gid:path}/mark-paid")
def mark_order_paid(order_gid: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """Settle a cash-on-delivery order once the courier has handed the money
    over.

    Shopify first, the local row after -- the same order the product edits use
    (`admin_products`): Shopify owns the financial status, and a local row
    saying "paid" that Shopify never accepted is the version staff would
    believe. There is no inverse; Shopify has no "mark as unpaid".
    """
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "orders")
        if refused is not None:
            return refused
        try:
            result = shopify_admin_orders.mark_as_paid(order_gid)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)
        if "error" in result:
            return JSONResponse(result, status_code=409)

        local = _local_order_for(db, order_gid)
        if local is not None:
            local.payment_status = "paid"
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


@router.get("/size-charts")
def list_size_charts(wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """The measured charts a product can be pointed at.

    Read from `data/size_charts.json`, which is where the bot reads them too
    -- offering staff a free-text chart id was offering them a way to type one
    that does not exist, which the bot then answers with "no chart".
    """
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "products")
        if refused is not None:
            return refused
    charts = size_charts_service.all_charts()
    return JSONResponse(
        {"charts": sorted(
            ({"id": cid, "title": c.get("title") or cid} for cid, c in charts.items()),
            key=lambda c: c["title"].casefold(),
        )}
    )


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------

#: The biggest picture this will take, decoded. Shopify's own limit is far
#: higher; this one is about the dashboard, where a 20 MB upload over a phone
#: connection is a staff member watching a spinner and giving up.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: What a browser is allowed to hand us. An allowlist rather than a check for
#: "image/", because the thing on the other end is Shopify's Files library and
#: an SVG there is a script that runs on the storefront's own origin.
ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: A staff member's filename ends up in a public url, so it is rebuilt from
#: what is safe rather than filtered for what is not. The dot is not in the
#: safe set either: the extension comes from the mime type, so every dot left
#: in the stem is one somebody typed -- `..` included.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_filename(name: str, mime: str) -> str:
    stem = _SAFE_NAME.sub("-", (name or "").rsplit(".", 1)[0]).strip("-")[:60]
    return f"{stem or 'upload'}{ALLOWED_UPLOAD_TYPES[mime]}"


@router.post("/uploads")
def upload_image(payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """One picture, base64 in a JSON body, on its way to Shopify.

    Base64 rather than multipart on purpose: `python-multipart` is not in the
    pinned requirements, and adding a dependency to production so a form can
    post a file is a bigger change than a third more bytes on an upload that
    happens a handful of times a week.

    `purpose` decides where it lands. A product photo is only *staged* -- the
    create call hands the resource url to `productCreateMedia` and the picture
    ends up owned by the product. A size chart goes into the Files library,
    because a `file_reference` metafield stores a file gid and nothing else.
    """
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "products")
        if refused is not None:
            return refused

    mime = (payload.get("content_type") or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_UPLOAD_TYPES:
        return JSONResponse(
            {"error": "bad_arguments", "detail": f"unsupported image type: {mime or 'unknown'}"},
            status_code=400,
        )
    try:
        data = base64.b64decode(payload.get("data") or "", validate=True)
    except (binascii.Error, ValueError):
        return JSONResponse({"error": "bad_arguments", "detail": "data is not base64"}, status_code=400)
    if not data:
        return JSONResponse({"error": "bad_arguments", "detail": "the file is empty"}, status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": "bad_arguments", "detail": f"the file is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"},
            status_code=413,
        )

    filename = _safe_filename(payload.get("filename") or "", mime)
    purpose = payload.get("purpose") or "product_image"
    try:
        client = get_admin_client()
        if purpose == "size_chart":
            uploaded = shopify_files.upload_to_files(
                client, filename, data, mime, alt=payload.get("alt") or filename
            )
            return JSONResponse({"file_gid": uploaded["id"], "url": uploaded["url"], "filename": filename})
        source = shopify_files.stage(client, filename, data, mime, resource="IMAGE")
    except shopify_files.FileUploadError as exc:
        return JSONResponse({"error": "upload_rejected", "detail": str(exc)}, status_code=409)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        return _outage(exc)
    return JSONResponse({"source": source, "filename": filename})


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
                images=payload.get("images") or None,
                size_chart_file_gid=payload.get("size_chart_file_gid") or None,
                size_chart_url=payload.get("size_chart_url") or None,
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
# customers: everyone who has ever bought -- read-only
# --------------------------------------------------------------------------
#
# One list, three tabs. "All" is every person the shop knows; "bot" is the
# ones who bought in a conversation; "web" is the ones who bought on the site.
# They are filtered views of the same rows, drawn by the same renderer, which
# is the point -- the two tabs used to come from two routes returning two
# different shapes, so switching tab changed which columns existed.
#
# The rows are Shopify's customers *plus* the bot customers Shopify has never
# heard of, merged on the phone number: the only identifier both sides
# reliably carry, normalised through `customer_ledger.phone_key` so
# `01067177128` and `+201067177128` are one person. A local row that matches a
# Shopify customer is dropped, not added -- every number below is computed
# from the orders, and counting the same order twice is exactly what a merge
# does if you let it.
#
# Those numbers come from `dashboard/customer_ledger.py`, folded out of the
# order list, not from `numberOfOrders`/`amountSpent`. Those are one number
# each where the owner asked for four (orders that stand, what they came to,
# orders cancelled, what those came to), they say nothing about which channel
# the person bought through, and they are blank on every customer the backfill
# created -- which is why the same person had a governorate on the bot tab and
# none on the store tab.


def _customer_ledger(db, q: str | None):
    """Everyone's order totals, plus the governorate their orders imply.

    Reads every order once. That is one more Shopify walk than the customer
    list alone, and it is what buys the four numbers and the channels -- the
    customer records cannot answer any of them. Deliberately not narrowed by
    `q`: a search for one person must still show that person's whole history,
    not the orders whose text happens to match their name.
    """
    orders, truncated = shopify_admin_orders.list_all_orders()
    local_by_id = _local_orders_for(db, [o["id"] for o in orders])
    for order in orders:
        order["channel"] = _order_channel(local_by_id.get(order["id"]), order)
    return customer_ledger.index(orders), truncated


def _local_clients(db, q: str | None) -> list[Client]:
    stmt = select(Client).order_by(Client.created_at.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Client.full_name.ilike(like), Client.phone.ilike(like)))
    return list(db.scalars(stmt).all())


def _shaped(client: Client) -> dict:
    """A bot customer in the same shape a Shopify one arrives in.

    `id` is the dashboard's own `c_<n>` public id rather than a Shopify gid,
    which is what tells the UI to open the local detail view -- there is no
    Shopify customer record to open.
    """
    return {
        "id": client.public_id,
        "client_id": client.client_id,
        "name": client.full_name,
        "email": client.email,
        "phone": client.phone,
        "governorate": client.governorate,
        "created_at": client.created_at.isoformat() if client.created_at else None,
        "source": "bot",
    }


@router.get("/customers")
def list_customers(
    q: str | None = Query(default=None),
    segment: str = Query(default="all", description="all | bot | web"),
    orders_count: int | None = Query(default=None, ge=0, description="filter by orders that stand"),
    orders_op: str = Query(default="eq", description="eq | gte -- how to read orders_count"),
    governorate: str | None = Query(default=None),
    sort: str = Query(default="recent", description="recent | orders_desc | orders_asc"),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    if segment not in customer_ledger.SEGMENTS:
        return _bad(f"segment must be one of {customer_ledger.SEGMENTS}")
    if orders_op not in customer_filters.ORDER_COUNT_OPS:
        return _bad("orders_op must be 'eq' or 'gte'")
    if sort not in customer_filters.CUSTOMER_SORTS:
        return _bad(f"sort must be one of {customer_filters.CUSTOMER_SORTS}")

    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "customers")
        if refused is not None:
            return refused
        options = customer_filters.governorate_options(db)
        try:
            # The whole customer list, always: the bot customers merged in
            # below are deduped against it by phone, and matching them against
            # page one alone would list anyone whose Shopify record sits on
            # page two twice, under both of their names. Filtering and
            # re-sorting need the whole list for their own reason -- page one
            # is the wrong denominator, see `list_all_customers`.
            customers, truncated = shopify_admin_customers.list_all_customers(query=q)
            ledger, orders_truncated = _customer_ledger(db, q)
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            return _outage(exc)

        clients = _local_clients(db, q)
        by_phone: dict[str, Client] = {}
        for client in clients:
            key = customer_ledger.phone_key(client.phone)
            if key:
                by_phone.setdefault(key, client)

        # One person can hold two Shopify customer records -- the shop has
        # exactly this: a website checkout that made a fresh record with no
        # phone, beside the record the backfill built for the same buyer from
        # their bot orders. The ledger already resolves both to one set of
        # totals (they share a phone on the shipping address), so listing both
        # rows would show the same orders twice under two spellings of one
        # name. The record carrying a phone is the one kept: it is what every
        # merge here, and the backfill itself, keys on.
        first_seen: dict[int, str] = {}
        for customer in sorted(customers, key=lambda c: not c.get("phone")):
            key = customer_ledger.phone_key(customer.get("phone"))
            stats = ledger.get(customer["id"]) or (ledger.get(key) if key else None)
            if stats is not None:
                first_seen.setdefault(id(stats), customer["id"])

        rows: list[dict] = []
        matched: set[str] = set()
        for customer in customers:
            customer.setdefault("source", "shopify")
            key = customer_ledger.phone_key(customer.get("phone"))
            local = by_phone.get(key) if key else None
            # Always present, even as None: the three tabs are drawn by one
            # renderer, and a key that appears only on some rows is a column
            # that appears only on some tabs.
            customer["client_id"] = local.client_id if local is not None else None
            if key:
                matched.add(key)
            # A governorate the person only ever gave the bot. Shopify's
            # record for the same buyer has no default address at all when the
            # backfill created it, and a blank column on one tab beside a
            # filled one on another is the bug that was reported.
            if local is not None and not customer.get("governorate"):
                customer["governorate"] = local.governorate
            stats = ledger.get(customer["id"]) or (ledger.get(key) if key else None)
            if stats is not None and first_seen[id(stats)] != customer["id"]:
                continue
            rows.append(customer_ledger.merge_into(customer, stats))

        for client in clients:
            key = customer_ledger.phone_key(client.phone)
            if key and key in matched:
                continue
            rows.append(customer_ledger.merge_into(_shaped(client), ledger.get(key) if key else None))

    rows = [c for c in rows if customer_ledger.in_segment(c, segment)]
    rows = customer_filters.apply_filters(
        rows,
        orders_count=orders_count,
        orders_op=orders_op,
        governorate=governorate,
        sort=sort,
    )

    return JSONResponse(
        {
            "customers": rows,
            # `has_next_page` is meaningless here -- this is the whole match,
            # not a page of it. `truncated` is the honest replacement: True
            # means a page cap was hit somewhere, so these numbers are a floor.
            "truncated": truncated or orders_truncated,
            "governorates": options,
            "segment": segment,
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

        # The orders come back in the Orders screen's own shape
        # (`admin_orders.order_summary`), so the drawer draws the table the
        # Orders tab draws and a row opens the same order.
        orders = customer["orders"]
        local_by_id = _local_orders_for(db, [o["id"] for o in orders])
        for order in orders:
            local = local_by_id.get(order["id"])
            order["local"] = _local_ref(local)
            order["channel"] = _order_channel(local, order)

        # Summed from the orders on screen, so the four KPIs above the table
        # are the table. `numberOfOrders` counts cancelled sales among the
        # orders, which is the one thing this screen must not do.
        customer_ledger.merge_into(customer, customer_ledger.summarise(orders))
        if not customer.get("governorate"):
            key = customer_ledger.phone_key(customer.get("phone"))
            local_client = next(
                (
                    c
                    for c in db.scalars(select(Client).where(Client.phone.isnot(None))).all()
                    if customer_ledger.phone_key(c.phone) == key and c.governorate
                ),
                None,
            ) if key else None
            if local_client is not None:
                customer["governorate"] = local_client.governorate
    return JSONResponse(customer)
