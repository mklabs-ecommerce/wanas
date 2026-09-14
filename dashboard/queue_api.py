"""The `item_swap` and `alert` staff-queue kinds, surfaced for the first time.

`item_add` is the newest of the three, and it exists because it was missing:
`item_swap` used to be the only post-order request type, so a customer asking
to *add* a piece to an order was filed as a request to replace one -- with a
garment he still wanted named as the thing to remove. Approving and refusing
are separate routes per kind on purpose (`approve-swap`, `approve-add`), so a
staff click cannot apply the wrong one to an item of the other kind.

`request_human` (`handoff`) had a dashboard from the start of this feature;
`item_swap` (`orders.apply_swap`) and `alert` (low stock, a modified order, a
failed confirmation...) have had full backend logic since Phase 1 and *no*
UI at all -- exactly the gap `handoff` was in before `web.py` existed. This
is that missing half for the other two kinds, not new business logic:
approving a swap calls the same `orders.apply_swap` a bot-driven approval
would, and an alert is acknowledged by resolving it, nothing more.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Cookie, Query
from fastapi.responses import JSONResponse

from common.events import after_commit
from dashboard.guard import require_permission
from domain.db import session_scope
from domain.models import Order, QueueKind, QueueStatus, StaffQueueItem, Variant
from domain.services import notifications, orders as orders_service, queues

router = APIRouter(prefix="/dashboard/api/queue", tags=["dashboard-queue"])

_KINDS = (QueueKind.ITEM_SWAP.value, QueueKind.ITEM_ADD.value, QueueKind.ALERT.value)

#: The kinds a customer is waiting on an answer to. Every resolution of one of
#: these reaches them; an `alert` has nobody behind it and stays silent.
_CUSTOMER_FACING_KINDS = (QueueKind.ITEM_SWAP.value, QueueKind.ITEM_ADD.value)


def _item_payload(item: StaffQueueItem) -> dict:
    return {
        "queue_id": item.queue_id,
        "kind": item.kind,
        "reason": item.reason,
        "summary": item.summary,
        "payload": item.payload,
        "order_id": item.order_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("")
def list_queue(
    kind: str | None = Query(default=None), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "queue")
        if refused is not None:
            return refused

        kinds = [kind] if kind in _KINDS else list(_KINDS)
        items = []
        for k in kinds:
            items.extend(queues.open_items(db, k))
        items.sort(key=lambda i: i.created_at, reverse=True)
        result = {"items": [_item_payload(i) for i in items]}
    return JSONResponse(result)


def _describe(db, item: StaffQueueItem) -> str:
    """What the customer called the thing they asked for, for the message
    that tells them what happened to it. Their own words if the request
    carried none, never a SKU -- `heart-top-s-black` means nothing to the
    person who asked for a black Heart Top in S."""
    payload = item.payload or {}
    variant_id = payload.get("to_variant_id")
    variant = db.get(Variant, variant_id) if variant_id else None
    if variant is not None:
        parts = [variant.product.name, variant.color, variant.size]
        return " ".join(str(p) for p in parts if p)
    return str(payload.get("note") or "").strip() or "القطعة"


def _tell_the_customer(db, item: StaffQueueItem, outcome: str, **kwargs) -> None:
    """Write the customer's line now, send it once this transaction commits.

    Both halves matter. The line goes in inside the transaction so a
    resolution that rolls back takes its own message with it; the send waits
    for the commit because the committing connection still holds the write
    lock -- the same split as `notifications.record_status_push` /
    `deliver_status_push`.
    """
    if outcome == "failed":
        # Staff pressed the button three times against the missing
        # `write_order_edits` scope. Three identical "we are looking into it"
        # messages is its own failure, so the holding line goes out once per
        # request and the flag rides on the item that caused it.
        payload = dict(item.payload or {})
        if payload.get("pending_notice_sent"):
            return
        payload["pending_notice_sent"] = True
        item.payload = payload

    order = db.get(Order, item.order_id) if item.order_id else None
    plan = notifications.record_request_resolution(
        db, item, outcome, order=order, item_label=_describe(db, item), **kwargs
    )
    after_commit(db, lambda: notifications.deliver_request_resolution(plan))


@router.post("/{queue_id}/resolve")
def resolve_queue_item(queue_id: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """The generic close: an alert acknowledged, or a post-order request
    declined.

    A declined request now *tells the customer*. It did not, and that was the
    worst of the three failures this round: he asked to add a piece, the bot
    told him the team would confirm, staff pressed reject, and he heard
    nothing ever again. An alert has no customer behind it and stays silent.
    """
    with session_scope() as db:
        staff, refused = require_permission(db, wanas_staff, "queue")
        if refused is not None:
            return refused
        pending = db.get(StaffQueueItem, queue_id)
        item = queues.resolve(db, queue_id, staff.staff_id, status=QueueStatus.REJECTED.value)
        if item is None:
            return JSONResponse({"error": "not_open"}, status_code=409)
        if pending is not None and pending.kind in _CUSTOMER_FACING_KINDS:
            _tell_the_customer(db, pending, "rejected")
    return JSONResponse({"ok": True})


@router.post("/{queue_id}/approve-add")
def approve_add(
    queue_id: str, payload: dict = Body(default={}), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    """Approve an `item_add` request: put the line on the order, take nothing
    off. Refuses a queue item of any other kind rather than coercing it --
    `apply_add` and `apply_swap` do different things to a customer's order,
    and the kind is the only thing that says which was asked for."""
    with session_scope() as db:
        staff, refused = require_permission(db, wanas_staff, "queue")
        if refused is not None:
            return refused

        item = db.get(StaffQueueItem, queue_id)
        if (
            item is None
            or item.kind != QueueKind.ITEM_ADD.value
            or item.status != QueueStatus.OPEN.value
        ):
            return JSONResponse({"error": "not_open"}, status_code=409)

        order = db.get(Order, item.order_id) if item.order_id else None
        if order is None:
            return JSONResponse({"error": "order_not_found"}, status_code=404)

        stored = item.payload or {}
        to_variant_id = payload.get("to_variant_id") or stored.get("to_variant_id")
        if not to_variant_id:
            detail = "to_variant_id is required (the customer's request did not name an item)"
            return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)
        quantity = int(payload.get("quantity") or stored.get("quantity") or 1)

        result = orders_service.apply_add(db, order, to_variant_id, quantity)
        if "error" in result:
            # The queue item stays open, and the customer is told we are on
            # it rather than left watching a promise expire.
            _tell_the_customer(db, item, "failed")
            return JSONResponse(result, status_code=409)

        queues.resolve(db, queue_id, staff.staff_id)
        _tell_the_customer(db, item, "approved")
    return JSONResponse(result)


@router.post("/{queue_id}/approve-swap")
def approve_swap(
    queue_id: str, payload: dict = Body(default={}), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    """Approve an `item_swap` request: apply it (same `orders.apply_swap` a
    stock/availability check already guards) and resolve the queue item only
    if it lands -- a rejected swap must not disappear from the queue."""
    with session_scope() as db:
        staff, refused = require_permission(db, wanas_staff, "queue")
        if refused is not None:
            return refused

        item = db.get(StaffQueueItem, queue_id)
        if (
            item is None
            or item.kind != QueueKind.ITEM_SWAP.value
            or item.status != QueueStatus.OPEN.value
        ):
            return JSONResponse({"error": "not_open"}, status_code=409)

        order = db.get(Order, item.order_id) if item.order_id else None
        if order is None:
            return JSONResponse({"error": "order_not_found"}, status_code=404)

        from_variant_id = (item.payload or {}).get("from_variant_id")
        to_variant_id = payload.get("to_variant_id") or (item.payload or {}).get("to_variant_id")
        if not from_variant_id or not to_variant_id:
            detail = "to_variant_id is required (the customer's request did not name a replacement)"
            return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)

        result = orders_service.apply_swap(db, order, from_variant_id, to_variant_id)
        if "error" in result:
            _tell_the_customer(db, item, "failed")
            return JSONResponse(result, status_code=409)

        queues.resolve(db, queue_id, staff.staff_id)
        _tell_the_customer(db, item, "approved")
    return JSONResponse(result)
