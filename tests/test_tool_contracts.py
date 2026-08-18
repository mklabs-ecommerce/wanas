"""Every refusal in 15-tool-contracts.md.

These are the guardrails. Each one needs a test proving it refuses, because
the whole design rests on tools refusing rather than the model behaving.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.models import Client, Order, QueueKind, ShippingRate, Variant
from backend.services import carts, identities, orders, queues
from chatbot.tools.base import REGISTRY, ToolContext, call_tool, load_all

load_all()

VARIANT = "wanas-hoodie-s-olive"
SOLD_OUT = "wanas-hoodie-m-olive"


@pytest.fixture()
def ctx(seeded):
    return ToolContext(session=seeded, channel="whatsapp", external_id="201000000001")


def call(ctx, name, **arguments):
    return call_tool(ctx, name, arguments)


# --- the seventeen --------------------------------------------------------


def test_exactly_eighteen_tools():
    """Every capability the bot has is on this list. A behaviour described in
    the docs with no tool here is a behaviour the bot cannot do."""
    assert sorted(REGISTRY) == sorted(
        [
            "get_categories",
            "get_products",
            "get_variants",
            "add_to_cart",
            "view_cart",
            "remove_from_cart",
            "get_size_chart",
            "get_shipping_fee",
            "ask_governorate",
            "confirm_order",
            "get_my_orders",
            "modify_order_quantity",
            "cancel_order",
            "request_item_swap",
            "submit_feedback",
            "request_human",
            "get_my_profile",
            "link_client",
        ]
    )
    assert len(REGISTRY) == 18


def test_every_tool_returns_an_object_never_prose(ctx):
    for name in REGISTRY:
        result = call_tool(ctx, name, {})
        assert isinstance(result, dict), name


# --- shared rules ---------------------------------------------------------


def test_unknown_arguments_are_rejected_not_ignored(ctx):
    result = call(ctx, "get_products", categoryy="T-Shirts")
    assert result["error"] == "bad_arguments"
    assert "categoryy" in result["detail"]


def test_the_model_cannot_supply_the_channel_identity(ctx):
    """If the model supplied it, a confused or manipulated model could read
    another customer's cart."""
    result = call(ctx, "view_cart", external_id="20199999999")
    assert result["error"] == "bad_arguments"


def test_missing_required_argument(ctx):
    assert call(ctx, "get_variants")["error"] == "bad_arguments"
    assert call(ctx, "get_size_chart", product_id="")["error"] == "bad_arguments"


def test_wrong_type_is_rejected(ctx):
    assert call(ctx, "add_to_cart", variant_id=VARIANT, quantity="two")["error"] == "bad_arguments"
    # ...but a stringified number that is actually right is coerced, not refused.
    assert "lines" in call(ctx, "add_to_cart", variant_id=VARIANT, quantity="2")


def test_unknown_tool(ctx):
    assert call_tool(ctx, "delete_everything", {})["error"] == "unknown_tool"


def test_a_crashing_tool_returns_an_error_not_an_exception(ctx, monkeypatch):
    """A crash inside a tool ends the customer's conversation, which is worse
    than any error message."""
    from chatbot.tools import catalog_tools

    def boom(*_a, **_k):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(catalog_tools.catalog, "get_categories", boom)
    result = call(ctx, "get_categories")
    assert result["error"] == "tool_failed"


# --- catalog --------------------------------------------------------------


def test_get_variants_product_not_found(ctx):
    assert call(ctx, "get_variants", product_id="no-such-product") == {
        "error": "product_not_found",
        "product_id": "no-such-product",
    }


def test_get_variants_returns_sold_out_and_the_offerable_subset(ctx):
    payload = call(ctx, "get_variants", product_id="wanas-hoodie")
    ids = {v["variant_id"] for v in payload["variants"]}
    assert SOLD_OUT in ids
    assert SOLD_OUT not in payload["in_stock"]


def test_stock_qty_is_returned_but_status_is_what_a_reply_uses(ctx):
    payload = call(ctx, "get_variants", product_id="wanas-hoodie")
    variant = payload["variants"][0]
    assert set(variant) == {
        "variant_id",
        "size",
        "color",
        "length",
        "price",
        "original_price",
        "on_sale",
        "stock_qty",
        "status",
    }


# --- cart -----------------------------------------------------------------


def test_add_to_cart_variant_not_found(ctx):
    """Never resolve to the nearest match: silently correcting an identifier
    ships the wrong size."""
    assert call(ctx, "add_to_cart", variant_id="wanas-hoodie-m-oliv") == {
        "error": "variant_not_found",
        "variant_id": "wanas-hoodie-m-oliv",
    }


def test_add_to_cart_out_of_stock_returns_alternatives(ctx):
    result = call(ctx, "add_to_cart", variant_id=SOLD_OUT)
    assert result["error"] == "out_of_stock"
    assert result["variant"] == {
        "variant_id": SOLD_OUT,
        "size": "M",
        "color": "Olive",
        "length": None,
    }
    assert result["alternatives"], "the recovery has to be one message, not a guess"
    # Same colour first -- and every alternative really is in stock.
    assert result["alternatives"][0]["color"] == "Olive"
    for alt in result["alternatives"]:
        assert ctx.session.get(Variant, alt["variant_id"]).stock_qty > 0


def test_add_to_cart_insufficient_stock(ctx):
    ctx.session.get(Variant, VARIANT).stock_qty = 3
    ctx.session.flush()
    assert call(ctx, "add_to_cart", variant_id=VARIANT, quantity=5) == {
        "error": "insufficient_stock",
        "available": 3,
    }


def test_add_to_cart_quantity_bounds(ctx):
    assert call(ctx, "add_to_cart", variant_id=VARIANT, quantity=0)["error"] == "bad_arguments"
    assert call(ctx, "add_to_cart", variant_id=VARIANT, quantity=11)["error"] == "bad_arguments"
    call(ctx, "add_to_cart", variant_id=VARIANT, quantity=9)
    # Topping up past the cap is refused rather than silently truncated.
    assert call(ctx, "add_to_cart", variant_id=VARIANT, quantity=5)["error"] == "bad_arguments"


def test_add_to_cart_does_not_reserve_stock(ctx):
    before = ctx.session.get(Variant, VARIANT).stock_qty
    call(ctx, "add_to_cart", variant_id=VARIANT, quantity=2)
    assert ctx.session.get(Variant, VARIANT).stock_qty == before


def test_cart_shape(ctx):
    cart = call(ctx, "add_to_cart", variant_id=VARIANT, quantity=2)
    assert set(cart) == {"lines", "item_count", "subtotal"}
    assert set(cart["lines"][0]) == {
        "line_id",
        "variant_id",
        "product_name",
        "size",
        "color",
        "length",
        "quantity",
        "unit_price",
        "unit_original_price",
        "line_total",
    }
    assert cart["subtotal"] == 1300
    assert cart["item_count"] == 2


def test_remove_from_cart_needs_exactly_one_argument(ctx):
    call(ctx, "add_to_cart", variant_id=VARIANT)
    assert call(ctx, "remove_from_cart")["error"] == "bad_arguments"
    assert call(ctx, "remove_from_cart", variant_id=VARIANT, clear_all=True)["error"] == "bad_arguments"


def test_removing_a_line_that_is_not_there_is_not_an_error(ctx):
    """The customer's intent is already satisfied."""
    call(ctx, "add_to_cart", variant_id=VARIANT)
    result = call(ctx, "remove_from_cart", variant_id="wanas-hoodie-l-black")
    assert "error" not in result
    assert len(result["lines"]) == 1


# --- sizing ---------------------------------------------------------------


def test_size_chart_shape_and_supplied_fields(ctx):
    chart = call(ctx, "get_size_chart", product_id="wanas-sweatpant")
    assert chart["has_chart"] is True
    assert chart["chart_id"] == "wide-leg-sweatpants"
    # Supplied by the tool, not stored per chart.
    assert chart["measurement_note"] == "Garment measurements laid flat, not body measurements."
    assert chart["length_specific"] is False
    assert chart["image"].startswith("data/size-charts/")


def test_size_chart_image_becomes_an_attachment(ctx):
    call(ctx, "get_size_chart", product_id="wanas-sweatpant")
    assert ctx.attachments == ["data/size-charts/wide-leg-sweatpants.png"]


def test_no_chart_returns_that_and_nothing_else(ctx):
    """Returning a neighbouring product's chart is the failure this shape
    exists to prevent."""
    product = ctx.session.get(__import__("backend.models", fromlist=["Product"]).Product, "wanas-hoodie")
    product.size_chart = None
    ctx.session.flush()
    assert call(ctx, "get_size_chart", product_id="wanas-hoodie") == {
        "has_chart": False,
        "product_id": "wanas-hoodie",
    }


def test_size_chart_unknown_product(ctx):
    assert call(ctx, "get_size_chart", product_id="nope")["error"] == "product_not_found"


def test_worker_jacket_chart_is_length_specific(ctx):
    chart = call(ctx, "get_size_chart", product_id="worker-jacket")
    assert chart["length_specific"] is True
    applies = {m["key"]: m.get("applies_to_length") for m in chart["measurements"]}
    assert applies["long_sleeve"] == "Long"


def test_tops_chart_has_no_xl(ctx):
    chart = call(ctx, "get_size_chart", product_id="feelin-fine-top")
    assert "XL" not in chart["sizes"]


# --- shipping -------------------------------------------------------------


def test_shipping_unknown_governorate_returns_the_valid_list(ctx):
    result = call(ctx, "get_shipping_fee", governorate="Atlantis")
    assert result["error"] == "unknown_governorate"
    assert len(result["valid"]) == 27


def test_shipping_no_rate_set(ctx):
    assert call(ctx, "get_shipping_fee", governorate="القاهرة") == {
        "error": "no_rate_set",
        "governorate": "Cairo",
    }


def test_shipping_fee_when_priced(ctx):
    ctx.session.get(ShippingRate, "Cairo").fee = 60
    ctx.session.flush()
    assert call(ctx, "get_shipping_fee", governorate="مصر الجديدة") == {"governorate": "Cairo", "fee": 60}


# --- confirm_order --------------------------------------------------------


def test_confirm_order_cart_empty(ctx):
    assert call(
        ctx,
        "confirm_order",
        customer_name="Omar",
        governorate="Cairo",
        address="1 St",
        contact_phone="01000000000",
    ) == {"error": "cart_empty"}


def test_confirm_order_missing_fields(ctx):
    call(ctx, "add_to_cart", variant_id=VARIANT)
    result = call(
        ctx,
        "confirm_order",
        customer_name="Omar",
        governorate="   ",
        address="1 St",
        contact_phone="01000000000",
    )
    # A blank is missing, not empty-but-present. And it never infers a
    # governorate from the address text.
    assert result == {"error": "missing_fields", "fields": ["governorate"]}

    # An absent field is the same refusal, with the same field list.
    assert call(ctx, "confirm_order", customer_name="Omar", address="1 St", contact_phone="0100") == {
        "error": "missing_fields",
        "fields": ["governorate"],
    }


def test_confirm_order_no_rate_set(ctx):
    call(ctx, "add_to_cart", variant_id=VARIANT)
    result = call(
        ctx,
        "confirm_order",
        customer_name="Omar",
        governorate="Cairo",
        address="1 St",
        contact_phone="01000000000",
    )
    assert result["error"] == "no_rate_set"


def test_confirm_order_items_out_of_stock_writes_nothing(ctx, shopify):
    ctx.session.get(ShippingRate, "Cairo").fee = 60
    call(ctx, "add_to_cart", variant_id=VARIANT, quantity=2)
    # Someone else buys it while the customer is typing their address -- on the
    # storefront, which is why the shelf that moves is Shopify's and not the
    # local row. Changing wanas.db here would be changing a copy.
    shopify.set(VARIANT, qty=1)
    ctx.session.get(Variant, VARIANT).stock_qty = 1
    ctx.session.flush()

    result = call(
        ctx,
        "confirm_order",
        customer_name="Omar",
        governorate="Cairo",
        address="1 St",
        contact_phone="01000000000",
    )
    assert result["error"] == "items_out_of_stock"
    assert result["items"][0]["available"] == 1
    assert ctx.session.scalar(select(Order)) is None
    assert ctx.session.get(Variant, VARIANT).stock_qty == 1


def test_confirm_order_client_blocked(ctx):
    ctx.session.get(ShippingRate, "Cairo").fee = 60
    blocked = Client(full_name="B", phone="01055556666", address="x", status="blocked")
    ctx.session.add(blocked)
    ctx.session.flush()
    identity = identities.get_or_create(ctx.session, ctx.channel, ctx.external_id)
    identity.client_id = blocked.client_id
    ctx.session.flush()

    call(ctx, "add_to_cart", variant_id=VARIANT)
    assert call(
        ctx,
        "confirm_order",
        customer_name="B",
        governorate="Cairo",
        address="x",
        contact_phone="01055556666",
    ) == {"error": "client_blocked"}


def test_confirm_order_success_shape(ctx):
    ctx.session.get(ShippingRate, "Cairo").fee = 60
    call(ctx, "add_to_cart", variant_id=VARIANT)
    result = call(
        ctx,
        "confirm_order",
        customer_name="Omar",
        governorate="القاهرة",
        address="1 St",
        contact_phone="01000000000",
    )
    assert set(result) == {
        "order_id",
        # Shopify's own "#1001" -- what the bot says out loud. `order_id` stays
        # the internal reference the other tools take.
        "reference",
        "status",
        "payment_method",
        "items",
        "subtotal",
        "discount_amount",
        "shipping_fee",
        "total",
    }
    assert result["order_id"].startswith("WNS-")
    assert result["reference"].startswith("#")
    assert result["status"] == "Confirmed"
    assert result["total"] == 710
    assert carts.is_empty(ctx.session, ctx.channel, ctx.external_id)


# --- after the order ------------------------------------------------------


@pytest.fixture()
def placed(ctx):
    ctx.session.get(ShippingRate, "Cairo").fee = 60
    call(ctx, "add_to_cart", variant_id=VARIANT, quantity=2)
    result = call(
        ctx,
        "confirm_order",
        customer_name="Omar",
        governorate="Cairo",
        address="1 St",
        contact_phone="01000000000",
    )
    return result["order_id"]


def test_get_my_orders_computes_modifiable(ctx, placed):
    payload = call(ctx, "get_my_orders")
    order = payload["orders"][0]
    assert order["order_id"] == placed
    assert order["modifiable"] is True
    assert order["items"][0]["variant_id"] == VARIANT


def test_get_my_orders_is_empty_for_an_unknown_customer(seeded):
    stranger = ToolContext(session=seeded, channel="whatsapp", external_id="20100000009")
    assert call(stranger, "get_my_orders") == {"orders": []}


def test_orders_are_scoped_to_the_identity(ctx, placed):
    """A confused or manipulated model must not reach another customer's
    order."""
    other = ToolContext(session=ctx.session, channel="whatsapp", external_id="20111111111")
    assert call(other, "cancel_order", order_id=placed) == {"error": "order_not_found", "order_id": placed}


def test_modify_order_quantity_refusals(ctx, placed):
    assert call(ctx, "modify_order_quantity", order_id="WNS-9999", variant_id=VARIANT, quantity=1)[
        "error"
    ] == "order_not_found"
    assert call(ctx, "modify_order_quantity", order_id=placed, variant_id="wanas-hoodie-l-black", quantity=1)[
        "error"
    ] == "line_not_found"
    assert call(ctx, "modify_order_quantity", order_id=placed, variant_id=VARIANT, quantity=11)[
        "error"
    ] == "bad_arguments"
    # The only line on the order: 0 would leave an empty ghost.
    assert call(ctx, "modify_order_quantity", order_id=placed, variant_id=VARIANT, quantity=0) == {
        "error": "would_empty_order",
        "order_id": placed,
    }


def test_modify_order_quantity_insufficient_stock(ctx, placed, shopify):
    # Emptied on Shopify, which is the shelf the increase is checked against.
    shopify.set(VARIANT, qty=0)
    ctx.session.get(Variant, VARIANT).stock_qty = 0
    ctx.session.flush()
    assert call(ctx, "modify_order_quantity", order_id=placed, variant_id=VARIANT, quantity=5) == {
        "error": "insufficient_stock",
        "available": 0,
    }


def test_modify_order_quantity_returns_the_new_total(ctx, placed):
    result = call(ctx, "modify_order_quantity", order_id=placed, variant_id=VARIANT, quantity=1)
    assert result["subtotal"] == 650
    assert result["shipping_fee"] == 60
    assert result["total"] == 710


def test_not_modifiable_after_shipped(ctx, placed):
    order = ctx.session.get(Order, placed)
    orders.advance_status(ctx.session, order, "Packed")
    orders.advance_status(ctx.session, order, "Shipped")
    ctx.session.flush()
    assert call(ctx, "modify_order_quantity", order_id=placed, variant_id=VARIANT, quantity=1) == {
        "error": "not_modifiable",
        "status": "Shipped",
    }
    assert call(ctx, "cancel_order", order_id=placed) == {"error": "not_modifiable", "status": "Shipped"}


def test_cancel_order_not_found(ctx):
    assert call(ctx, "cancel_order", order_id="WNS-4242")["error"] == "order_not_found"


def test_request_item_swap_queues_and_never_applies(ctx, placed):
    result = call(ctx, "request_item_swap", order_id=placed, from_variant_id=VARIANT, note="عايز Black")
    assert result["queued"] is True
    assert result["request_id"].startswith("SWAP-")

    # The order is untouched: staff have to check stock and decide.
    order = ctx.session.get(Order, placed)
    assert order.items[0].variant_id == VARIANT
    assert order.items[0].quantity == 2
    assert any(i.kind == QueueKind.ITEM_SWAP.value for i in queues.open_items(ctx.session))


def test_request_item_swap_refusals(ctx, placed):
    assert call(ctx, "request_item_swap", order_id="WNS-1", from_variant_id=VARIANT)["error"] == "order_not_found"
    assert call(ctx, "request_item_swap", order_id=placed, from_variant_id="wanas-hoodie-l-black")[
        "error"
    ] == "line_not_found"


def test_submit_feedback_refusals_and_success(ctx, placed):
    assert call(ctx, "submit_feedback", order_id="WNS-1", rating=5)["error"] == "order_not_found"
    assert call(ctx, "submit_feedback", order_id=placed, rating=5)["error"] == "not_delivered"

    order = ctx.session.get(Order, placed)
    for status in ("Packed", "Shipped", "Delivered"):
        orders.advance_status(ctx.session, order, status)
    ctx.session.flush()

    assert call(ctx, "submit_feedback", order_id=placed, rating=9)["error"] == "bad_arguments"
    assert call(ctx, "submit_feedback", order_id=placed, rating=5, text="جامد")["saved"] is True
    assert call(ctx, "submit_feedback", order_id=placed, rating=4)["error"] == "already_rated"


# --- escalation and identity ---------------------------------------------


def test_request_human_pauses_the_conversation(ctx):
    result = call(ctx, "request_human", reason="customer_asked", summary="wants a person")
    assert result == {"queued": True, "conversation_paused": True}
    assert identities.is_paused(ctx.session, ctx.channel, ctx.external_id) is True
    assert queues.open_items(ctx.session, QueueKind.HANDOFF.value)


def test_request_human_rejects_an_invented_reason(ctx):
    assert call(ctx, "request_human", reason="bored", summary="x")["error"] == "bad_arguments"


def test_get_my_profile_unknown_is_not_an_error(ctx):
    """The normal state for a first-time customer, and the case every
    implementation forgets."""
    assert call(ctx, "get_my_profile") == {"known": False, "pending_link": None}


def test_get_my_profile_known(ctx, placed):
    profile = call(ctx, "get_my_profile")
    assert profile["known"] is True
    assert profile["client_id"].startswith("c_")
    assert profile["governorate"] == "Cairo"
    assert profile["email"] is None


def test_pending_link_is_masked_and_never_leaks_internals(ctx):
    existing = Client(full_name="Mona Adel", phone="201000000001", address="Old", status="active")
    ctx.session.add(existing)
    ctx.session.flush()
    identity = identities.get_or_create(ctx.session, ctx.channel, ctx.external_id)
    identities.detect_pending_link_from_external_id(ctx.session, identity)

    profile = call(ctx, "get_my_profile")
    assert profile["known"] is False
    assert profile["pending_link"]["masked_name"] == "M… A…"
    assert profile["pending_link"]["matched_on"] == "phone"
    assert "_client_pk" not in profile["pending_link"]


def test_link_client_requires_a_pending_link(ctx):
    assert call(ctx, "link_client", confirmed=True) == {"error": "no_pending_link"}


def test_link_client_confirmed_and_declined(ctx):
    existing = Client(
        full_name="Mona Adel", phone="201000000001", address="Old", governorate="Giza", status="active"
    )
    ctx.session.add(existing)
    ctx.session.flush()
    identity = identities.get_or_create(ctx.session, ctx.channel, ctx.external_id)
    identities.detect_pending_link_from_external_id(ctx.session, identity)

    result = call(ctx, "link_client", confirmed=True)
    assert result["linked"] is True
    assert result["address"] == "Old"
    assert identity.client_id == existing.client_id
    assert identity.pending_link is None

    # Declining leaves them separate.
    identities.note_pending_link(ctx.session, identity, existing, "phone")
    identity.client_id = None
    assert call(ctx, "link_client", confirmed=False) == {"linked": False}
    assert identity.client_id is None
