"""The WhatsApp-side Customers view: local `Client` rows.

Deliberately separate from `dashboard/shopify_api.py`'s Shopify
customers -- merging the two would silently under- or double-count exactly
the way a Postgres-only revenue number would (see `docs/ARCHITECTURE.md`).
A `Client` exists only once the bot's own checkout created one
(`backend/services/orders.py::_client_for_order`); a customer who has only
ever ordered on the website has no row here at all, and that is correct, not
a bug to fix by inventing one.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Query
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select

from common.money import money
from dashboard.guard import staff_for, unauthenticated
from domain.db import session_scope
from domain.models import Client, Order

router = APIRouter(prefix="/dashboard/api/customers", tags=["dashboard-customers"])

MAX_CUSTOMERS = 300


def _summary(client: Client) -> dict:
    return {
        "client_id": client.client_id,
        "public_id": client.public_id,
        "full_name": client.full_name,
        "phone": client.phone,
        "email": client.email,
        "governorate": client.governorate,
        "status": client.status,
        "created_at": client.created_at.isoformat() if client.created_at else None,
    }


@router.get("")
def list_local_customers(
    q: str | None = Query(default=None), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

        stmt = select(Client).order_by(Client.created_at.desc()).limit(MAX_CUSTOMERS)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(Client.full_name.ilike(like), Client.phone.ilike(like)))
        clients = db.scalars(stmt).all()
        result = {"customers": [_summary(c) for c in clients]}
    return JSONResponse(result)


@router.get("/{client_id}")
def local_customer_detail(client_id: int, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

        client = db.get(Client, client_id)
        if client is None:
            return JSONResponse({"error": "customer_not_found"}, status_code=404)

        orders = db.scalars(
            select(Order).where(Order.client_id == client_id).order_by(Order.placed_at.desc())
        ).all()
        result = {
            **_summary(client),
            "address": client.address,
            "orders": [
                {
                    "order_id": o.order_id,
                    "reference": o.shopify_order_name or o.order_id,
                    "status": o.status,
                    "total": money(o.total),
                    "placed_at": o.placed_at.isoformat() if o.placed_at else None,
                    "source_channel": o.source_channel,
                }
                for o in orders
            ],
        }
    return JSONResponse(result)
