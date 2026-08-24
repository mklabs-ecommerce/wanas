"""Ordering and after-the-order tools.

confirm_order, get_my_orders, modify_order_quantity, cancel_order,
request_item_swap, submit_feedback.

The status rule lives here, not in the prompt: modify and cancel check the
order's status themselves and refuse a shipped order regardless of how the
request was phrased. A prompt rule would be a suggestion the model could talk
itself out of.
"""

from __future__ import annotations

from chatbot.tools.base import ToolContext, tool
from domain.models import Variant
from domain.services import orders
from domain.services.notifications import item_swap_requested


@tool(
    "confirm_order",
    "Place the order. This is the only tool that writes one, and it re-checks live stock itself. "
    "Do not tell the customer an order was placed until this returns an order_id. Requires the "
    "customer's name, the governorate (a picked value, not inferred from the address text), the "
    "full address and a contact phone. Cash on delivery only.",
    properties={
        "customer_name": {"type": "string"},
        "governorate": {"type": "string", "description": "From the fixed list; ask, do not infer."},
        "address": {"type": "string", "description": "Street, building, apartment, landmark."},
        "contact_phone": {"type": "string"},
        "email": {"type": "string", "description": "Optional; the WhatsApp flow does not ask for one."},
    },
    required=("customer_name", "governorate", "address", "contact_phone"),
    missing_error="missing_fields",
)
def confirm_order(
    ctx: ToolContext,
    customer_name: str,
    governorate: str,
    address: str,
    contact_phone: str,
    email: str | None = None,
) -> dict:
    return orders.place_order(
        ctx.session,
        channel=ctx.channel,
        external_id=ctx.external_id,
        customer_name=customer_name,
        governorate=governorate,
        address=address,
        contact_phone=contact_phone,
        email=email,
    )


@tool(
    "get_my_orders",
    "This customer's orders, open ones by default. Call it before any change so you can ask which "
    "order rather than guessing by recency. `modifiable` is computed for you -- do not work it out "
    "from the status yourself.",
    properties={"include_closed": {"type": "boolean", "description": "Include delivered and cancelled."}},
)
def get_my_orders(ctx: ToolContext, include_closed: bool | None = None) -> dict:
    rows = orders.orders_for_identity(
        ctx.session, ctx.channel, ctx.external_id, include_closed=bool(include_closed)
    )
    return {"orders": [orders.order_summary(order) for order in rows]}


@tool(
    "modify_order_quantity",
    "Set one line of an existing order to an absolute quantity, 0-10. 0 removes the line; removing "
    "the last line is refused -- cancel the order instead. Returns the order with a recalculated "
    "total, which you must read back to the customer: on cash on delivery a silently changed "
    "amount becomes an argument at the door. The shipping fee is never re-quoted.",
    properties={
        "order_id": {"type": "string"},
        "variant_id": {"type": "string", "description": "A line already on that order."},
        "quantity": {"type": "integer", "description": "Absolute new quantity, 0-10."},
    },
    required=("order_id", "variant_id", "quantity"),
)
def modify_order_quantity(ctx: ToolContext, order_id: str, variant_id: str, quantity: int) -> dict:
    quantity = int(quantity)
    if quantity < 0 or quantity > 10:
        return {"error": "bad_arguments", "detail": "quantity must be between 0 and 10"}

    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}
    return orders.modify_quantity(ctx.session, order, variant_id, quantity)


@tool(
    "cancel_order",
    "Cancel an order that has not shipped. Returns all its stock and notifies staff.",
    properties={"order_id": {"type": "string"}},
    required=("order_id",),
)
def cancel_order(ctx: ToolContext, order_id: str) -> dict:
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}
    return orders.cancel(ctx.session, order, by="customer")


@tool(
    "request_item_swap",
    "Queue a request to swap one item on an order for a different one. This never applies the swap "
    "-- staff check stock for the replacement and decide. Tell the customer someone will confirm; "
    "do not imply it is done. to_variant_id is optional when the customer only described what they "
    "want.",
    properties={
        "order_id": {"type": "string"},
        "from_variant_id": {"type": "string", "description": "The line they want to replace."},
        "to_variant_id": {"type": "string", "description": "Optional replacement, if known."},
        "note": {"type": "string", "description": "What the customer said they want."},
    },
    required=("order_id", "from_variant_id"),
)
def request_item_swap(
    ctx: ToolContext,
    order_id: str,
    from_variant_id: str,
    to_variant_id: str | None = None,
    note: str | None = None,
) -> dict:
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}

    line = next((item for item in order.items if item.variant_id == from_variant_id), None)
    if line is None:
        return {"error": "line_not_found", "variant_id": from_variant_id}

    to_variant = ctx.session.get(Variant, to_variant_id) if to_variant_id else None
    summary = f"{order.order_id}: swap {line.product_name} ({line.color}, {line.size})"
    if to_variant is not None:
        summary += f" → {to_variant.product.name} ({to_variant.color}, {to_variant.size})"
    elif note:
        summary += f" → {note}"

    request_id = item_swap_requested(
        ctx.session,
        order,
        {
            "channel": ctx.channel,
            "external_id": ctx.external_id,
            "from_variant_id": from_variant_id,
            "to_variant_id": to_variant_id,
            "note": note,
        },
        summary,
    )
    return {"queued": True, "request_id": request_id}


@tool(
    "submit_feedback",
    "Record the customer's star rating for a delivered order. Rating is 1-5; free text is optional "
    "on top. Only a delivered order can be rated, and only once.",
    properties={
        "order_id": {"type": "string"},
        "rating": {"type": "integer", "description": "1-5."},
        "text": {"type": "string"},
    },
    required=("order_id", "rating"),
)
def submit_feedback(ctx: ToolContext, order_id: str, rating: int, text: str | None = None) -> dict:
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}
    return orders.submit_feedback(ctx.session, order, int(rating), text)
