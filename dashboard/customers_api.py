"""The WhatsApp-side Customers view: local `Client` rows.

Deliberately separate from `dashboard/shopify_api.py`'s Shopify
customers -- merging the two would silently under- or double-count exactly
the way a Postgres-only revenue number would (see `docs/ARCHITECTURE.md`).
A `Client` exists only once the bot's own checkout created one
(`domain/services/orders.py::_client_for_order`); a customer who has only
ever ordered on the website has no row here at all, and that is correct, not
a bug to fix by inventing one.

It offers the same filters the store tab does, through
`dashboard/customer_filters.py`: the order count and governorate a bot
customer has are on their own rows, so the questions are answerable on both
sides even though the data is not shared. What it does *not* do is share the
store tab's numbers -- `order_count` here counts this customer's orders
through the bot, which is the smaller number on purpose.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select

from common.money import money
from dashboard import customer_filters
from dashboard.guard import require_permission
from domain.db import session_scope
from domain.models import Client, Order

router = APIRouter(prefix="/dashboard/api/customers", tags=["dashboard-customers"])

MAX_CUSTOMERS = 300


def _summary(client: Client, order_count: int = 0) -> dict:
    return {
        "client_id": client.client_id,
        "public_id": client.public_id,
        "full_name": client.full_name,
        "phone": client.phone,
        "email": client.email,
        "governorate": client.governorate,
        "status": client.status,
        "created_at": client.created_at.isoformat() if client.created_at else None,
        # Named to match the store tab's field so one filter helper reads
        # both. It counts *bot* orders only, which is the honest number for
        # this list -- see the module docstring.
        "order_count": order_count,
    }


@router.get("")
def list_local_customers(
    q: str | None = Query(default=None),
    orders_count: int | None = Query(default=None, ge=0, description="filter by bot order count"),
    orders_op: str = Query(default="eq", description="eq | gte -- how to read orders_count"),
    governorate: str | None = Query(default=None),
    sort: str = Query(default="recent", description="recent | orders_desc | orders_asc"),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    if orders_op not in customer_filters.ORDER_COUNT_OPS:
        return JSONResponse({"error": "bad_request", "detail": "orders_op must be 'eq' or 'gte'"},
                            status_code=400)
    if sort not in customer_filters.CUSTOMER_SORTS:
        return JSONResponse(
            {"error": "bad_request",
             "detail": f"sort must be one of {customer_filters.CUSTOMER_SORTS}"},
            status_code=400,
        )

    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "customers")
        if refused is not None:
            return refused

        options = customer_filters.governorate_options(db)

        # Counted in one grouped query rather than per row: a hundred
        # customers should not be a hundred round trips, and the count is now
        # a filterable field rather than something the detail view alone
        # needed. An outer join keeps a customer whose only orders were
        # cancelled and deleted -- they are still a customer, with zero.
        counts = select(
            Client.client_id.label("cid"), func.count(Order.order_id).label("n")
        ).outerjoin(Order, Order.client_id == Client.client_id).group_by(Client.client_id).subquery()

        stmt = (
            select(Client, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.cid == Client.client_id)
            .order_by(Client.created_at.desc())
            .limit(MAX_CUSTOMERS)
        )
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(Client.full_name.ilike(like), Client.phone.ilike(like)))
        rows = db.execute(stmt).all()
        customers = [_summary(client, count) for client, count in rows]

    customers = customer_filters.apply_filters(
        customers,
        orders_count=orders_count,
        orders_op=orders_op,
        governorate=governorate,
        sort=sort,
    )

    return JSONResponse(
        {
            "customers": customers,
            "governorates": options,
            "filters": {
                "orders_count": orders_count,
                "orders_op": orders_op,
                "governorate": governorate,
                "sort": sort,
            },
        }
    )


@router.get("/{client_id}")
def local_customer_detail(client_id: int, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "customers")
        if refused is not None:
            return refused

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
