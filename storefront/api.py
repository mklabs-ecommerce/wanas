"""The web store's `/api/*` surface -- see web/BACKEND-FOR-FRONTEND.md for the
contract this follows. A thin layer over backend/services: no business rule
lives here, only request parsing, the guest-session cookie, and reshaping
service output into the JSON the storefront reads.

Guest identity: `channel="website"`, `external_id` a random token in an
httpOnly cookie -- the same (channel, external_id) key backend/services
already uses for carts and channel identities, so a website guest is a first
-class identity next to a WhatsApp one, not a bolt-on.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.db import get_session
from backend.models import Channel, Product, Variant
from backend.money import money
from backend.services import carts, catalog, identities, shipping, size_charts
from backend.services import orders as orders_service

CHANNEL = Channel.WEBSITE.value
GUEST_COOKIE = "wns_session"
GUEST_COOKIE_MAX_AGE = 60 * 60 * 24 * 180  # 180 days

router = APIRouter(prefix="/api", tags=["storefront"])


def get_external_id(request: Request, response: Response) -> str:
    """Issues the guest cookie on first visit; every later call reuses it."""
    eid = request.cookies.get(GUEST_COOKIE)
    if not eid:
        eid = secrets.token_urlsafe(24)
    response.set_cookie(
        GUEST_COOKIE, eid, max_age=GUEST_COOKIE_MAX_AGE, httponly=True, samesite="lax"
    )
    return eid


def _asset(path: str | None) -> str | None:
    """`data/images/x/01.jpg` -> `/data/images/x/01.jpg`, matching the static
    mounts registered in app.py."""
    if not path:
        return None
    return path if path.startswith("/") else "/" + path


def _assets(paths) -> list[str]:
    return [_asset(p) for p in (paths or [])]


def _variant_out(v: Variant) -> dict:
    return {
        "variant_id": v.variant_id,
        "product_id": v.product_id,
        "size": v.size,
        "color": v.color,
        "length": v.length,
        "price": money(v.price),
        "original_price": money(v.original_price),
        "on_sale": bool(v.on_sale),
        "status": v.status,
    }


def _product_out(p: Product) -> dict:
    prices = [v.price for v in p.variants]
    originals = [v.original_price for v in p.variants]
    return {
        "product_id": p.product_id,
        "name": p.name,
        "category": p.category,
        "department": p.department,
        "style": list(p.style or []),
        "collection": p.collection,
        "colors": list(p.colors or []),
        "sizes": list(p.sizes or []),
        "lengths": list(p.lengths or []),
        # Never a single number -- three products cost more in one colour, so
        # price_from/price_to is the honest listing-card claim; the exact
        # price is only known once a variant is selected on the product page.
        "price_from": money(min(prices)) if prices else 0,
        "price_to": money(max(prices)) if prices else 0,
        "original_price_to": money(max(originals)) if originals else 0,
        "on_sale": any(v.on_sale for v in p.variants),
        "any_in_stock": any(v.stock_qty > 0 for v in p.variants),
        "has_size_chart": p.size_chart is not None,
        "description": p.description or "",
        "images": _assets(p.images),
        "color_images": {c: _assets(xs) for c, xs in (p.color_images or {}).items()},
    }


# ---------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------


@router.get("/categories")
def api_categories(session: Session = Depends(get_session)):
    return catalog.get_categories(session)


@router.get("/products")
def api_products(
    category: str | None = None,
    style: str | None = None,
    department: str | None = None,
    collection: str | None = None,
    q: str | None = None,
    session: Session = Depends(get_session),
):
    stmt = select(Product).options(selectinload(Product.variants)).order_by(Product.name)
    if category:
        stmt = stmt.where(func.lower(Product.category) == category.lower())
    if department:
        stmt = stmt.where(func.lower(Product.department) == department.lower())
    if collection:
        stmt = stmt.where(func.lower(Product.collection) == collection.lower())

    products = list(session.scalars(stmt).all())

    if style:
        wanted = style.lower()
        products = [p for p in products if any(wanted == s.lower() for s in (p.style or []))]
    if q:
        needle = q.lower().split()

        def _matches(p: Product) -> bool:
            haystack = " ".join(
                [
                    p.name, p.category, p.department,
                    " ".join(p.style or []), " ".join(p.colors or []),
                    p.collection or "", p.description or "",
                ]
            ).lower()
            return all(token in haystack for token in needle)

        products = [p for p in products if _matches(p)]

    return {"products": [_product_out(p) for p in products], "count": len(products)}


@router.get("/products/{product_id}")
def api_product_detail(product_id: str, session: Session = Depends(get_session)):
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "product_not_found")
    out = _product_out(product)
    variants = sorted(product.variants, key=lambda v: (v.color or "", v.length or "", v.size))
    out["variants"] = [_variant_out(v) for v in variants]
    return out


@router.get("/products/{product_id}/size-chart")
def api_size_chart(product_id: str, session: Session = Depends(get_session)):
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "product_not_found")
    chart = size_charts.get_chart(product.size_chart)
    if chart is None:
        return {"has_chart": False}
    out = dict(chart)
    out["image"] = _asset(out.get("image"))
    out["has_chart"] = True
    return out


# ---------------------------------------------------------------------
# Shipping
# ---------------------------------------------------------------------


@router.get("/governorates")
def api_governorates(session: Session = Depends(get_session)):
    return [
        {
            "key": r.governorate,
            "label_ar": r.label_ar,
            "fee": money(r.fee) if r.fee is not None else None,
            "available": r.fee is not None,
        }
        for r in shipping.all_rates(session)
    ]


@router.get("/shipping-fee")
def api_shipping_fee(governorate: str, session: Session = Depends(get_session)):
    resolved = shipping.resolve(session, governorate)
    if resolved is None:
        return {"error": "no_rate_set", "known_governorate": False}
    fee = shipping.get_fee(session, resolved)
    if fee is None:
        return {"error": "no_rate_set", "known_governorate": True}
    return {"fee": money(fee)}


# ---------------------------------------------------------------------
# Cart -- server-held, keyed by the guest session, not client state synced
# later (web/BACKEND-FOR-FRONTEND.md, Cart).
# ---------------------------------------------------------------------


def _cart_out(session: Session, eid: str) -> dict:
    payload = carts.cart_payload(session, CHANNEL, eid)
    for line in payload["lines"]:
        variant = session.get(Variant, line["variant_id"])
        image = None
        if variant is not None:
            product = variant.product
            imgs = (product.color_images or {}).get(variant.color) or product.images or []
            image = _asset(imgs[0]) if imgs else None
        line["image"] = image
        line["status"] = variant.status if variant is not None else "sold_out"
    return payload


@router.get("/cart")
def api_get_cart(eid: str = Depends(get_external_id), session: Session = Depends(get_session)):
    return _cart_out(session, eid)


class CartAddBody(BaseModel):
    variant_id: str
    quantity: int = 1


@router.post("/cart/items")
def api_add_cart_item(
    body: CartAddBody, eid: str = Depends(get_external_id), session: Session = Depends(get_session)
):
    variant = session.get(Variant, body.variant_id)
    if variant is None:
        raise HTTPException(404, "variant_not_found")
    if body.quantity < 1:
        raise HTTPException(422, "quantity must be at least 1")
    carts.add(session, CHANNEL, eid, body.variant_id, body.quantity)
    return _cart_out(session, eid)


class CartQtyBody(BaseModel):
    quantity: int


@router.patch("/cart/items/{line_id}")
def api_set_cart_item(
    line_id: int, body: CartQtyBody, eid: str = Depends(get_external_id), session: Session = Depends(get_session)
):
    carts.set_quantity(session, CHANNEL, eid, line_id, body.quantity)
    return _cart_out(session, eid)


@router.delete("/cart/items/{line_id}")
def api_remove_cart_item(
    line_id: int, eid: str = Depends(get_external_id), session: Session = Depends(get_session)
):
    carts.remove_line(session, CHANNEL, eid, line_id)
    return _cart_out(session, eid)


# ---------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------


class OrderBody(BaseModel):
    customer_name: str
    governorate: str
    address: str
    contact_phone: str
    email: str | None = None


@router.post("/orders")
def api_place_order(
    body: OrderBody, eid: str = Depends(get_external_id), session: Session = Depends(get_session)
):
    result = orders_service.place_order(
        session,
        channel=CHANNEL,
        external_id=eid,
        customer_name=body.customer_name,
        governorate=body.governorate,
        address=body.address,
        contact_phone=body.contact_phone,
        email=(body.email or None),
    )
    if "error" not in result:
        # A pending link is only ever created as a side effect of placing an
        # order (backend/services/identities.py) -- there is no earlier hook
        # to surface it from, so the confirmation screen is where "is this
        # you?" belongs for the website.
        identity = identities.get(session, CHANNEL, eid)
        if identity is not None and identity.pending_link:
            result["pending_link"] = {
                "matched_on": identity.pending_link.get("matched_on"),
                "masked_name": identity.pending_link.get("masked_name"),
            }
    return result


class LinkBody(BaseModel):
    confirmed: bool


@router.post("/clients/link")
def api_link_client(
    body: LinkBody, eid: str = Depends(get_external_id), session: Session = Depends(get_session)
):
    identity = identities.get(session, CHANNEL, eid)
    if identity is None or not identity.pending_link:
        raise HTTPException(400, "no_pending_link")
    if body.confirmed:
        identities.link(session, identity, identity.pending_link["_client_pk"])
    else:
        identities.decline_link(session, identity)
    return {"linked": body.confirmed}
