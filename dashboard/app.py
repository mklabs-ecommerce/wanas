"""The staff dashboard.

Server-rendered templates -- a separate frontend framework is not in scope.

Every route below depends on `current_staff`, so there is no path into the
dashboard that skips the login, and every write records who did it in the
audit log. With one role, attribution is the only control there is.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.db import get_session
from backend.events import after_commit
from backend.models import (
    STATUS_SEQUENCE,
    ChannelIdentity,
    Client,
    Order,
    OrderFeedback,
    Product,
    QueueKind,
    QueueStatus,
    ShippingRate,
    Staff,
    StaffQueueItem,
    Variant,
    utcnow,
)
from backend.money import money
from backend.services import audit, identities, inventory, notifications, orders, queues
from backend.services.auth import authenticate
from backend.services.size_charts import all_charts
from chatbot import session as session_store
from chatbot.runtime import staff_reply
from dashboard.auth import CSRF_FIELD, clear_session, current_staff, issue_session, redirect, verify_csrf

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["money"] = money

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def render(request: Request, staff: Staff, db: Session, template: str, **context) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        template,
        {
            "staff": staff,
            "csrf": getattr(request.state, "csrf", ""),
            "csrf_field": CSRF_FIELD,
            "counts": queues.open_counts(db),
            "message": request.query_params.get("msg"),
            **context,
        },
    )


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    has_staff = db.scalar(select(func.count()).select_from(Staff)) > 0
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {
            "error": request.query_params.get("error"),
            # There is no default account and no seeded password: a shared or
            # guessable login destroys the attribution this model relies on.
            "has_staff": has_staff,
        },
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
):
    staff = authenticate(db, username, password)
    if staff is None:
        return redirect("/dashboard/login?error=1")
    response = redirect("/dashboard")
    issue_session(response, staff)
    return response


@router.post("/logout")
def logout(request: Request, staff: Staff = Depends(current_staff)):
    response = redirect("/dashboard/login")
    clear_session(response)
    return response


# --------------------------------------------------------------------------
# Home
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def home(
    request: Request, staff: Staff = Depends(current_staff), db: Session = Depends(get_session)
) -> HTMLResponse:
    open_orders = db.scalars(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.status.in_(["Confirmed", "Packed", "Shipped"]))
        .order_by(Order.placed_at.desc())
        .limit(10)
    ).all()
    low_stock = db.scalars(
        select(Variant)
        .options(selectinload(Variant.product))
        .where(Variant.stock_qty > 0, Variant.stock_qty <= Variant.low_stock_threshold)
        .limit(20)
    ).all()
    unpriced = db.scalar(select(func.count()).select_from(ShippingRate).where(ShippingRate.fee.is_(None)))
    return render(
        request,
        staff,
        db,
        "home.html",
        open_orders=open_orders,
        low_stock=low_stock,
        unpriced=unpriced,
        recent_queue=queues.open_items(db)[:10],
    )


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------


@router.get("/orders", response_class=HTMLResponse)
def orders_list(
    request: Request,
    status: str | None = None,
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stmt = select(Order).options(selectinload(Order.items), selectinload(Order.client))
    if status:
        stmt = stmt.where(Order.status == status)
    rows = db.scalars(stmt.order_by(Order.placed_at.desc())).all()
    return render(
        request, staff, db, "orders.html", orders=rows, status=status, statuses=list(STATUS_SEQUENCE) + ["Cancelled"]
    )


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(
    request: Request,
    order_id: str,
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    order = db.get(Order, order_id)
    if order is None:
        return redirect("/dashboard/orders?msg=Order+not+found")
    feedback = db.scalar(select(OrderFeedback).where(OrderFeedback.order_id == order_id))
    next_status = None
    if order.status in STATUS_SEQUENCE and order.status != STATUS_SEQUENCE[-1]:
        next_status = STATUS_SEQUENCE[STATUS_SEQUENCE.index(order.status) + 1]
    return render(
        request,
        staff,
        db,
        "order_detail.html",
        order=order,
        feedback=feedback,
        next_status=next_status,
        history=audit.for_entity(db, "order", order_id),
    )


@router.post("/orders/{order_id}/status")
def order_advance(
    request: Request,
    order_id: str,
    new_status: str = Form(...),
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    verify_csrf(request, csrf_token)
    order = db.get(Order, order_id)
    if order is None:
        return redirect("/dashboard/orders?msg=Order+not+found")

    before = order.status
    result = orders.advance_status(db, order, new_status)
    if "error" in result:
        return redirect(f"/dashboard/orders/{order_id}?msg={result['error']}")

    audit.record(
        db,
        staff_id=staff.staff_id,
        action="order.status_changed",
        entity="order",
        entity_id=order_id,
        before={"status": before},
        after={"status": order.status},
    )
    return redirect(f"/dashboard/orders/{order_id}?msg=Status+is+now+{order.status}")


@router.post("/orders/{order_id}/cancel")
def order_cancel(
    request: Request,
    order_id: str,
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    verify_csrf(request, csrf_token)
    order = db.get(Order, order_id)
    if order is None:
        return redirect("/dashboard/orders?msg=Order+not+found")

    before = order.status
    result = orders.cancel(db, order, by=f"staff:{staff.username}", notify_customer=True)
    if "error" in result:
        return redirect(f"/dashboard/orders/{order_id}?msg={result['error']}")

    audit.record(
        db,
        staff_id=staff.staff_id,
        action="order.cancelled",
        entity="order",
        entity_id=order_id,
        before={"status": before},
        after={"status": "Cancelled"},
    )
    return redirect(f"/dashboard/orders/{order_id}?msg=Cancelled+and+stock+returned")


# --------------------------------------------------------------------------
# Products and inventory
# --------------------------------------------------------------------------


@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    q: str | None = None,
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stmt = select(Product).options(selectinload(Product.variants)).order_by(Product.category, Product.name)
    if q:
        stmt = stmt.where(func.lower(Product.name).like(f"%{q.lower()}%"))
    return render(request, staff, db, "products.html", products=db.scalars(stmt).all(), q=q or "")


@router.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(
    request: Request,
    product_id: str,
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if product is None:
        return redirect("/dashboard/products?msg=Product+not+found")

    # The grid staff live in. Two-dimensional per product (colour x size, and
    # colour x size x length for the Worker Jacket) -- "we ran out of olive in
    # M" happens far more often than a product selling out entirely.
    sizes = sorted({v.size for v in product.variants}, key=lambda s: ["S", "M", "L", "XL"].index(s) if s in ["S", "M", "L", "XL"] else 99)
    rows: dict[tuple, dict] = {}
    for variant in product.variants:
        rows.setdefault((variant.color, variant.length), {})[variant.size] = variant

    return render(
        request,
        staff,
        db,
        "product_detail.html",
        product=product,
        sizes=sizes,
        rows=sorted(rows.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")),
        charts=sorted(all_charts()),
    )


@router.post("/products/{product_id}/variant")
def variant_edit(
    request: Request,
    product_id: str,
    variant_id: str = Form(...),
    stock_qty: int = Form(...),
    low_stock_threshold: int = Form(...),
    price: float = Form(...),
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    """One row editable without a form per variant -- a product has up to 16 of
    them, and staff who find it fiddly stop keeping it accurate, which is the
    only failure mode that matters on this screen."""
    verify_csrf(request, csrf_token)
    variant = db.get(Variant, variant_id)
    if variant is None or variant.product_id != product_id:
        return redirect(f"/dashboard/products/{product_id}?msg=Variant+not+found")

    before = {
        "stock_qty": variant.stock_qty,
        "low_stock_threshold": variant.low_stock_threshold,
        "price": float(variant.price),
    }
    # Staff edits go outside the order-driven Inventory path, but still through
    # the Inventory service so nothing else writes stock_qty directly.
    inventory.set_stock(db, variant_id, stock_qty)
    variant.low_stock_threshold = max(0, int(low_stock_threshold))
    variant.price = price
    variant.on_sale = float(price) < float(variant.original_price)
    db.flush()

    audit.record(
        db,
        staff_id=staff.staff_id,
        action="variant.edited",
        entity="variant",
        entity_id=variant_id,
        before=before,
        after={
            "stock_qty": variant.stock_qty,
            "low_stock_threshold": variant.low_stock_threshold,
            "price": float(variant.price),
        },
    )
    return redirect(f"/dashboard/products/{product_id}?msg=Saved+{variant_id}")


@router.post("/products/{product_id}/size-chart")
def assign_size_chart(
    request: Request,
    product_id: str,
    size_chart: str = Form(""),
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    verify_csrf(request, csrf_token)
    product = db.get(Product, product_id)
    if product is None:
        return redirect("/dashboard/products?msg=Product+not+found")

    before = product.size_chart
    # "No chart" stays a supported state: a new product lands unmapped and
    # someone has to fix that without a deploy.
    product.size_chart = size_chart or None
    db.flush()
    audit.record(
        db,
        staff_id=staff.staff_id,
        action="product.size_chart_assigned",
        entity="product",
        entity_id=product_id,
        before={"size_chart": before},
        after={"size_chart": product.size_chart},
    )
    return redirect(f"/dashboard/products/{product_id}?msg=Size+chart+updated")


@router.get("/size-charts", response_class=HTMLResponse)
def size_charts(
    request: Request, staff: Staff = Depends(current_staff), db: Session = Depends(get_session)
) -> HTMLResponse:
    products = db.scalars(select(Product).order_by(Product.category, Product.name)).all()
    charts = all_charts()
    return render(
        request,
        staff,
        db,
        "size_charts.html",
        products=products,
        charts=charts,
        chart_ids=sorted(charts),
        unmapped=[p for p in products if not p.size_chart],
    )


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------


@router.get("/clients", response_class=HTMLResponse)
def clients_list(
    request: Request,
    q: str | None = None,
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stmt = select(Client).order_by(Client.client_id.desc())
    if q:
        stmt = stmt.where(func.lower(Client.full_name).like(f"%{q.lower()}%") | Client.phone.like(f"%{q}%"))
    return render(request, staff, db, "clients.html", clients=db.scalars(stmt).all(), q=q or "")


@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(
    request: Request,
    client_id: int,
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    client = db.get(Client, client_id)
    if client is None:
        return redirect("/dashboard/clients?msg=Client+not+found")
    client_orders = db.scalars(
        select(Order).where(Order.client_id == client_id).order_by(Order.placed_at.desc())
    ).all()
    feedback = db.scalars(select(OrderFeedback).where(OrderFeedback.client_id == client_id)).all()
    linked = db.scalars(select(ChannelIdentity).where(ChannelIdentity.client_id == client_id)).all()
    return render(
        request,
        staff,
        db,
        "client_detail.html",
        client=client,
        orders=client_orders,
        feedback=feedback,
        identities=linked,
    )


@router.post("/clients/{client_id}/status")
def client_block(
    request: Request,
    client_id: int,
    new_status: str = Form(...),
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    verify_csrf(request, csrf_token)
    client = db.get(Client, client_id)
    if client is None:
        return redirect("/dashboard/clients?msg=Client+not+found")
    before = client.status
    # Blocked is reserved for abuse cases, not an approval gate.
    client.status = "blocked" if new_status == "blocked" else "active"
    db.flush()
    audit.record(
        db,
        staff_id=staff.staff_id,
        action="client.status_changed",
        entity="client",
        entity_id=str(client_id),
        before={"status": before},
        after={"status": client.status},
    )
    return redirect(f"/dashboard/clients/{client_id}?msg=Status+is+now+{client.status}")


# --------------------------------------------------------------------------
# Shipping rates
# --------------------------------------------------------------------------


@router.get("/shipping", response_class=HTMLResponse)
def shipping_rates(
    request: Request, staff: Staff = Depends(current_staff), db: Session = Depends(get_session)
) -> HTMLResponse:
    from backend.services.shipping import all_rates

    return render(request, staff, db, "shipping.html", rates=all_rates(db))


@router.post("/shipping")
def shipping_update(
    request: Request,
    governorate: str = Form(...),
    fee: str = Form(""),
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    verify_csrf(request, csrf_token)
    rate = db.get(ShippingRate, governorate)
    if rate is None:
        return redirect("/dashboard/shipping?msg=Unknown+governorate")

    before = float(rate.fee) if rate.fee is not None else None
    # Clearing a fee back to blank is allowed and meaningful: it stops orders
    # for that governorate rather than shipping them at a stale price.
    rate.fee = float(fee) if fee.strip() else None
    rate.updated_at = utcnow()
    rate.updated_by = staff.staff_id
    db.flush()
    audit.record(
        db,
        staff_id=staff.staff_id,
        action="shipping.fee_set",
        entity="shipping_rate",
        entity_id=governorate,
        before={"fee": before},
        after={"fee": float(rate.fee) if rate.fee is not None else None},
    )
    return redirect(f"/dashboard/shipping?msg=Saved+{governorate}")


# --------------------------------------------------------------------------
# Queues
# --------------------------------------------------------------------------


@router.get("/queues", response_class=HTMLResponse)
def queue_list(
    request: Request,
    kind: str | None = None,
    show: str = "open",
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stmt = select(StaffQueueItem)
    if kind:
        stmt = stmt.where(StaffQueueItem.kind == kind)
    if show == "open":
        stmt = stmt.where(StaffQueueItem.status == QueueStatus.OPEN.value)
    items = db.scalars(stmt.order_by(StaffQueueItem.created_at.desc()).limit(200)).all()
    return render(
        request,
        staff,
        db,
        "queues.html",
        items=items,
        kind=kind,
        show=show,
        kinds=[k.value for k in QueueKind],
    )


@router.get("/queues/{queue_id}", response_class=HTMLResponse)
def queue_detail(
    request: Request,
    queue_id: str,
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = db.get(StaffQueueItem, queue_id)
    if item is None:
        return redirect("/dashboard/queues?msg=Not+found")

    conversation = []
    paused = False
    if item.channel and item.external_id:
        # Full message history attached, so staff are not picking up
        # context-free.
        conversation = session_store.load(db, item.channel, item.external_id)
        paused = identities.is_paused(db, item.channel, item.external_id)

    order = db.get(Order, item.order_id) if item.order_id else None
    to_variant = None
    if item.kind == QueueKind.ITEM_SWAP.value and item.payload.get("to_variant_id"):
        to_variant = db.get(Variant, item.payload["to_variant_id"])

    return render(
        request,
        staff,
        db,
        "queue_detail.html",
        item=item,
        conversation=conversation,
        paused=paused,
        order=order,
        to_variant=to_variant,
    )


@router.post("/queues/{queue_id}/reply")
def queue_reply(
    request: Request,
    queue_id: str,
    text: str = Form(...),
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    """Reply to the customer as the shop, without handing the conversation
    back to the bot. Resolving is a separate, deliberate action."""
    verify_csrf(request, csrf_token)
    item = db.get(StaffQueueItem, queue_id)
    if item is None or not item.channel:
        return redirect("/dashboard/queues?msg=Not+found")

    order = db.get(Order, item.order_id) if item.order_id else None
    phone = order.contact_phone if order else item.external_id
    # Sent only once this transaction actually commits -- staff_reply and the
    # audit row below can still fail, and a WhatsApp message describing a
    # reply that never got saved is worse than a delayed one.
    after_commit(db, lambda: notifications.get_sender().send_text(phone, text))
    staff_reply(db, item.channel, item.external_id, text)
    audit.record(
        db,
        staff_id=staff.staff_id,
        action="queue.replied",
        entity="staff_queue",
        entity_id=queue_id,
        after={"text": text},
    )
    return redirect(f"/dashboard/queues/{queue_id}?msg=Reply+sent")


@router.post("/queues/{queue_id}/resolve")
def queue_resolve(
    request: Request,
    queue_id: str,
    decision: str = Form("resolved"),
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    verify_csrf(request, csrf_token)
    item = db.get(StaffQueueItem, queue_id)
    if item is None:
        return redirect("/dashboard/queues?msg=Not+found")

    target = QueueStatus.REJECTED.value if decision == "rejected" else QueueStatus.RESOLVED.value
    resolved = queues.resolve(db, queue_id, staff.staff_id, status=target)
    if resolved is None:
        # Whichever staff action lands first wins; the second is told rather
        # than double-applying.
        return redirect(f"/dashboard/queues/{queue_id}?msg=Already+resolved+by+someone+else")

    # Resolving a handoff is the only thing that un-pauses a conversation.
    if item.kind == QueueKind.HANDOFF.value and item.channel:
        identities.unpause(db, item.channel, item.external_id)

    audit.record(
        db,
        staff_id=staff.staff_id,
        action=f"queue.{target}",
        entity="staff_queue",
        entity_id=queue_id,
        after={"kind": item.kind, "reason": item.reason},
    )
    return redirect(f"/dashboard/queues/{queue_id}?msg=Marked+{target}")


@router.post("/queues/{queue_id}/apply-swap")
def queue_apply_swap(
    request: Request,
    queue_id: str,
    to_variant_id: str = Form(...),
    csrf_token: str = Form(..., alias=CSRF_FIELD),
    staff: Staff = Depends(current_staff),
    db: Session = Depends(get_session),
):
    """Approving a swap runs the same apply-automatically logic as a quantity
    change -- and can still fail on stock, which is why staff check first."""
    verify_csrf(request, csrf_token)
    item = db.get(StaffQueueItem, queue_id)
    if item is None or item.kind != QueueKind.ITEM_SWAP.value:
        return redirect("/dashboard/queues?msg=Not+found")
    if item.status != QueueStatus.OPEN.value:
        return redirect(f"/dashboard/queues/{queue_id}?msg=Already+resolved+by+someone+else")

    order = db.get(Order, item.order_id) if item.order_id else None
    if order is None:
        return redirect(f"/dashboard/queues/{queue_id}?msg=Order+not+found")

    result = orders.apply_swap(db, order, item.payload.get("from_variant_id"), to_variant_id)
    if "error" in result:
        return redirect(f"/dashboard/queues/{queue_id}?msg={result['error']}")

    queues.resolve(db, queue_id, staff.staff_id)
    audit.record(
        db,
        staff_id=staff.staff_id,
        action="swap.approved",
        entity="order",
        entity_id=order.order_id,
        before={"from": item.payload.get("from_variant_id")},
        after={"to": to_variant_id, "total": result["total"]},
    )
    # Cash on delivery: the customer has to hear the new number. Deferred to
    # after_commit like every other outbound message -- sending it now, before
    # the transaction is guaranteed to land, is how a customer ends up told
    # about a swap the database never actually recorded.
    phone = order.contact_phone
    order_id = order.order_id
    total = result["total"]
    after_commit(
        db,
        lambda: notifications.get_sender().send_text(
            phone, f"تم تعديل طلبك {order_id}. الإجمالي الجديد {total} جنيه (الدفع عند الاستلام)."
        ),
    )
    return redirect(f"/dashboard/queues/{queue_id}?msg=Swap+applied")


# --------------------------------------------------------------------------
# Audit log -- attribution is only useful if it is readable
# --------------------------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def audit_log(
    request: Request, staff: Staff = Depends(current_staff), db: Session = Depends(get_session)
) -> HTMLResponse:
    entries = audit.recent(db, limit=200)
    people = {s.staff_id: s.username for s in db.scalars(select(Staff)).all()}
    return render(request, staff, db, "audit.html", entries=entries, people=people)
