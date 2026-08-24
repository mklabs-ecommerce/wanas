"""The `item_swap` and `alert` staff-queue kinds, surfaced for the first time.

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

from dashboard.guard import staff_for, unauthenticated
from domain.db import session_scope
from domain.models import Order, QueueKind, QueueStatus, StaffQueueItem
from domain.services import orders as orders_service, queues

router = APIRouter(prefix="/dashboard/api/queue", tags=["dashboard-queue"])

_KINDS = (QueueKind.ITEM_SWAP.value, QueueKind.ALERT.value)


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
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

        kinds = [kind] if kind in _KINDS else list(_KINDS)
        items = []
        for k in kinds:
            items.extend(queues.open_items(db, k))
        items.sort(key=lambda i: i.created_at, reverse=True)
        result = {"items": [_item_payload(i) for i in items]}
    return JSONResponse(result)


@router.post("/{queue_id}/resolve")
def resolve_queue_item(queue_id: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """The generic close: an alert acknowledged, or a swap request declined."""
    with session_scope() as db:
        staff = staff_for(db, wanas_staff)
        if staff is None:
            return unauthenticated()
        item = queues.resolve(db, queue_id, staff.staff_id, status=QueueStatus.REJECTED.value)
        if item is None:
            return JSONResponse({"error": "not_open"}, status_code=409)
    return JSONResponse({"ok": True})


@router.post("/{queue_id}/approve-swap")
def approve_swap(
    queue_id: str, payload: dict = Body(default={}), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    """Approve an `item_swap` request: apply it (same `orders.apply_swap` a
    stock/availability check already guards) and resolve the queue item only
    if it lands -- a rejected swap must not disappear from the queue."""
    with session_scope() as db:
        staff = staff_for(db, wanas_staff)
        if staff is None:
            return unauthenticated()

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
            return JSONResponse(result, status_code=409)

        queues.resolve(db, queue_id, staff.staff_id)
    return JSONResponse(result)
