"""Cart tools: add_to_cart, view_cart, remove_from_cart."""

from __future__ import annotations

from backend.config import settings
from backend.models import Variant
from backend.services import carts, catalog
from chatbot.tools.base import ToolContext, tool

MAX_PER_LINE = settings.max_quantity_per_line


@tool(
    "add_to_cart",
    "Add a variant to the cart. Takes a variant_id from get_variants -- never one you constructed. "
    "Quantity defaults to 1 and is capped at 10 per line. If the variant is sold out this returns "
    "the in-stock siblings of the same product in `alternatives`; those are the only substitutes "
    "you may offer. Call it as soon as the customer has decided -- 'add it', 'I'll take one', "
    "'حطهولي' -- and do not ask them to confirm a decision they have already made. The only reason "
    "to ask anything first is a missing size, colour or length you genuinely need to identify the "
    "variant.",
    properties={
        "variant_id": {"type": "string", "description": "Exactly as returned by get_variants."},
        "quantity": {"type": "integer", "description": "1-10, defaults to 1."},
    },
    required=("variant_id",),
)
def add_to_cart(ctx: ToolContext, variant_id: str, quantity: int | None = None) -> dict:
    quantity = 1 if quantity is None else int(quantity)
    if quantity < 1:
        return {"error": "bad_arguments", "detail": "quantity must be at least 1"}
    if quantity > MAX_PER_LINE:
        # Above the cap it is a wholesale enquiry, and refusing is the cheapest
        # guard against a typo emptying the shelf. Capping silently would ship
        # 10 when 12 was asked for.
        return {"error": "bad_arguments", "detail": f"quantity is capped at {MAX_PER_LINE} per line"}

    variant = ctx.session.get(Variant, variant_id)
    if variant is None:
        # Never resolve to the nearest match. Silently correcting an identifier
        # ships the wrong size.
        return {"error": "variant_not_found", "variant_id": variant_id}

    if variant.stock_qty <= 0:
        return {
            "error": "out_of_stock",
            "variant": {
                "variant_id": variant.variant_id,
                "size": variant.size,
                "color": variant.color,
                "length": variant.length,
            },
            "alternatives": catalog.alternatives_for(ctx.session, variant),
        }

    existing = carts.get_line(ctx.session, ctx.channel, ctx.external_id, variant_id)
    already = existing.quantity if existing else 0
    if already + quantity > MAX_PER_LINE:
        return {
            "error": "bad_arguments",
            "detail": f"that line would exceed the cap of {MAX_PER_LINE} units",
        }
    if variant.stock_qty < already + quantity:
        return {"error": "insufficient_stock", "available": variant.stock_qty}

    carts.add(ctx.session, ctx.channel, ctx.external_id, variant_id, quantity)
    return carts.cart_payload(ctx.session, ctx.channel, ctx.external_id)


@tool("view_cart", "What is currently in the customer's cart, with a subtotal.")
def view_cart(ctx: ToolContext) -> dict:
    return carts.cart_payload(ctx.session, ctx.channel, ctx.external_id)


@tool(
    "remove_from_cart",
    "Remove one line by line_id, or by variant_id, or empty the cart with clear_all. Give exactly "
    "one of the three.",
    properties={
        "line_id": {"type": "integer"},
        "variant_id": {"type": "string"},
        "clear_all": {"type": "boolean"},
    },
)
def remove_from_cart(
    ctx: ToolContext,
    line_id: int | None = None,
    variant_id: str | None = None,
    clear_all: bool | None = None,
) -> dict:
    given = [arg for arg in (line_id, variant_id, clear_all if clear_all else None) if arg is not None]
    if len(given) != 1:
        return {"error": "bad_arguments", "detail": "give exactly one of line_id, variant_id, clear_all"}

    if clear_all:
        carts.clear(ctx.session, ctx.channel, ctx.external_id)
    elif line_id is not None:
        carts.remove_line(ctx.session, ctx.channel, ctx.external_id, int(line_id))
    else:
        carts.remove_variant(ctx.session, ctx.channel, ctx.external_id, variant_id)

    # Removing a line that is not there returns the cart unchanged rather than
    # an error: the customer's intent is already satisfied.
    return carts.cart_payload(ctx.session, ctx.channel, ctx.external_id)
