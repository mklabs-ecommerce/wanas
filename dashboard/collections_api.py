"""The Collections section: Shopify collections, listed and edited.

A sibling router like every other area of this dashboard (see `web.py`'s
docstring), on the same staff-cookie guard, calling only
`integrations/shopify/admin_collections.py` -- no vendor HTTP lives here.

Collections have no wanas.db mirror on purpose: `Product.collection` is a
free-text merchandising label the bot's search reads, not a foreign key into
Shopify's collection objects. Nothing in this file writes to Postgres, and
membership edits are refused for smart collections, whose rules would undo
them on the next re-evaluation.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Cookie, Query
from fastapi.responses import JSONResponse

from dashboard.guard import require_permission
from domain.db import session_scope
from integrations.shopify import admin_collections
from integrations.shopify.catalog import ShopifyConfigError, ShopifyUnavailable

router = APIRouter(prefix="/dashboard/api/shopify/collections", tags=["dashboard-collections"])


def _outage(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "store_unavailable", "detail": str(exc)}, status_code=503)


def _rejected(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "collection_rejected", "detail": str(exc)}, status_code=409)


#: This whole router is the Collections section, so one permission covers it.
PERMISSION = "collections"


def _refused(wanas_staff: str | None) -> JSONResponse | None:
    """None when this account may work collections, otherwise the 401/403 to
    return. Opens its own short session because every route below does its
    Shopify call outside one -- see `dashboard/guard.py`."""
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, PERMISSION)
        return refused


@router.get("")
def list_collections(
    q: str | None = Query(default=None), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    refused = _refused(wanas_staff)
    if refused is not None:
        return refused
    try:
        result = admin_collections.list_collections(query=q)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        return _outage(exc)
    return JSONResponse(result)


@router.post("")
def create_collection(
    payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    refused = _refused(wanas_staff)
    if refused is not None:
        return refused

    title = (payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "bad_arguments", "detail": "title is required"}, status_code=400)
    try:
        result = admin_collections.create_collection(
            title=title, description_html=payload.get("description_html") or ""
        )
    except admin_collections.CollectionRejected as exc:
        return _rejected(exc)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        return _outage(exc)
    return JSONResponse(result, status_code=201)


@router.get("/{collection_gid:path}")
def collection_detail(
    collection_gid: str, wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    refused = _refused(wanas_staff)
    if refused is not None:
        return refused
    try:
        collection = admin_collections.get_collection(collection_gid)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        return _outage(exc)
    if collection is None:
        return JSONResponse({"error": "collection_not_found"}, status_code=404)
    return JSONResponse(collection)


@router.post("/{collection_gid:path}/update")
def update_collection(
    collection_gid: str, payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    refused = _refused(wanas_staff)
    if refused is not None:
        return refused
    try:
        result = admin_collections.update_collection(
            collection_gid,
            title=(payload.get("title") or None),
            description_html=payload.get("description_html"),
        )
    except admin_collections.CollectionRejected as exc:
        return _rejected(exc)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        return _outage(exc)
    return JSONResponse(result)


def _membership(collection_gid: str, payload: dict, *, add: bool) -> JSONResponse:
    product_ids = [p for p in (payload.get("product_ids") or []) if isinstance(p, str) and p.strip()]
    if not product_ids:
        detail = "product_ids must be a non-empty list of Shopify product gids"
        return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)

    try:
        # A smart collection's members come from its rules; a hand edit here
        # would be reverted by Shopify on the next re-evaluation, so it is
        # refused rather than accepted and silently lost.
        collection = admin_collections.get_collection(collection_gid)
        if collection is None:
            return JSONResponse({"error": "collection_not_found"}, status_code=404)
        if collection["smart"]:
            detail = "this is a smart collection -- its members come from its rules"
            return JSONResponse({"error": "smart_collection", "detail": detail}, status_code=409)

        action = admin_collections.add_products if add else admin_collections.remove_products
        result = action(collection_gid, product_ids)
    except admin_collections.CollectionRejected as exc:
        return _rejected(exc)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        return _outage(exc)
    return JSONResponse(result)


@router.post("/{collection_gid:path}/products/add")
def add_products(
    collection_gid: str, payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    refused = _refused(wanas_staff)
    if refused is not None:
        return refused
    return _membership(collection_gid, payload, add=True)


@router.post("/{collection_gid:path}/products/remove")
def remove_products(
    collection_gid: str, payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    refused = _refused(wanas_staff)
    if refused is not None:
        return refused
    return _membership(collection_gid, payload, add=False)
