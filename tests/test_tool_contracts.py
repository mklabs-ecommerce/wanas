"""Every refusal in 15-tool-contracts.md.

These are the guardrails. Each one needs a test proving it refuses, because
the whole design rests on tools refusing rather than the model behaving.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from assistant.tools.base import REGISTRY, ToolContext, call_tool, load_all
from domain.models import Client, Order, QueueKind, ShippingRate, Variant, utcnow
from domain.services import (
    carts,
    identities,
    orders,
    queues,
    waitlist,
)

load_all()

VARIANT = "wanas-hoodie-s-olive"
SOLD_OUT = "wanas-hoodie-m-olive"


@pytest.fixture()
def ctx(seeded):
    return ToolContext(session=seeded, channel="whatsapp", external_id="201000000001")


def call(ctx, name, **arguments):
    return call_tool(ctx, name, arguments)


# --- the seventeen --------------------------------------------------------


def test_exactly_nineteen_tools():
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
            "get_return_terms",
            "request_item_swap",
            "submit_feedback",
            "request_human",
            "get_my_profile",
            "link_client",
        ]
    )
    assert len(REGISTRY) == 19


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
    # Blank counts as missing, not as a value: whitespace is not a product id.
    assert call(ctx, "get_variants", product_id="")["error"] == "bad_arguments"


def test_an_omitted_product_with_nothing_to_resolve_to_is_its_own_answer(ctx):
    """`get_size_chart` no longer requires `product_id` -- it may be left out
    to mean the product under discussion. With an empty history there is
    nothing to resolve to, and the answer is a distinct code rather than
    `bad_arguments`: the model has not malformed the call, the conversation
    simply has not settled on a product, and what that needs is a question to
    the customer."""
    assert call(ctx, "get_size_chart")["error"] == "no_product_in_context"
    assert call(ctx, "get_size_chart", product_id="  ")["error"] == "no_product_in_context"


def test_wrong_type_is_rejected(ctx):
    assert call(ctx, "add_to_cart", variant_id=VARIANT, quantity="two")["error"] == "bad_arguments"
    # ...but a stringified number that is actually right is coerced, not refused.
    assert "lines" in call(ctx, "add_to_cart", variant_id=VARIANT, quantity="2")


def test_unknown_tool(ctx):
    assert call_tool(ctx, "delete_everything", {})["error"] == "unknown_tool"


def test_a_crashing_tool_returns_an_error_not_an_exception(ctx, monkeypatch):
    """A crash inside a tool ends the customer's conversation, which is worse
    than any error message."""
    from assistant.tools import catalog_tools

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


def test_add_to_cart_out_of_stock_joins_the_waitlist(ctx):
    """The only signal this app gets that someone wanted a sold-out variant
    -- see domain/services/waitlist.py."""
    from domain.services import waitlist

    call(ctx, "add_to_cart", variant_id=SOLD_OUT)
    entries = waitlist.open_entries(ctx.session)
    assert len(entries) == 1
    assert entries[0].variant_id == SOLD_OUT
    assert entries[0].channel == ctx.channel
    assert entries[0].external_id == ctx.external_id

    # A second failed attempt by the same customer does not duplicate the row.
    call(ctx, "add_to_cart", variant_id=SOLD_OUT)
    assert len(waitlist.open_entries(ctx.session)) == 1


def test_add_to_cart_insufficient_stock(ctx, shopify):
    # Set on the shelf, not on the wanas.db row: what a cart may hold follows
    # Shopify's count, the same as every price and status the bot quotes.
    shopify.set(VARIANT, qty=3)
    assert call(ctx, "add_to_cart", variant_id=VARIANT, quantity=5) == {
        "error": "insufficient_stock",
        "available": 3,
    }


def test_add_to_cart_follows_shopify_not_the_stale_local_row(ctx, shopify):
    """A wanas.db row stuck at zero must not refuse a size Shopify is selling.

    This is the whole of the phantom-restock bug in one assertion: the refusal
    below used to happen, it joined the stock waitlist, and half an hour later
    the customer was told an item that had never left the shelf was "back in
    stock".
    """
    ctx.session.get(Variant, VARIANT).stock_qty = 0
    ctx.session.flush()
    shopify.set(VARIANT, qty=4)

    result = call(ctx, "add_to_cart", variant_id=VARIANT)
    assert "error" not in result
    assert waitlist.open_entries(ctx.session) == []


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


def test_an_uploaded_chart_picture_is_a_chart_with_nothing_to_quote(ctx):
    """What the dashboard's "upload a size chart" produces: a picture and no
    published measurements. It is still a chart -- the customer reads it the
    same way they would on the storefront -- but `sizes` is empty, so there is
    nothing here for the model to read a number off."""
    Product = __import__("domain.models", fromlist=["Product"]).Product
    product = ctx.session.get(Product, "wanas-hoodie")
    product.size_chart = None
    product.size_chart_image = "https://cdn.example/hoodie-chart.png"
    ctx.session.flush()

    chart = call(ctx, "get_size_chart", product_id="wanas-hoodie")

    assert chart["has_chart"] is True
    assert chart["image_only"] is True
    assert chart["sizes"] == {} and chart["measurements"] == []
    assert chart["measurement_note"]
    assert ctx.attachments == ["https://cdn.example/hoodie-chart.png"]


def test_no_chart_returns_that_and_nothing_else(ctx):
    """Returning a neighbouring product's chart is the failure this shape
    exists to prevent."""
    product = ctx.session.get(__import__("domain.models", fromlist=["Product"]).Product, "wanas-hoodie")
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


def test_different_products_never_return_each_others_chart(ctx):
    """A customer asking about one product must never receive another
    product's chart_id, image, or measurements -- the exact failure
    AGENTS.md calls out as causing returns."""
    sweatpant = call(ctx, "get_size_chart", product_id="wanas-sweatpant")
    hoodie = call(ctx, "get_size_chart", product_id="wanas-hoodie")
    assert sweatpant["chart_id"] != hoodie["chart_id"]
    assert sweatpant["image"] != hoodie["image"]
    assert sweatpant["sizes"] != hoodie["sizes"]

    crewneck = call(ctx, "get_size_chart", product_id="wanas-crewneck")
    assert crewneck["chart_id"] not in {sweatpant["chart_id"], hoodie["chart_id"]}
    assert crewneck["image"] not in {sweatpant["image"], hoodie["image"]}


def test_boxy_wns_tee_and_ringer_tee_have_distinct_charts(ctx):
    """Regression: these two products used to share one chart_id
    ("ringer-boxy-tee") even though their real measurements differ, so a
    Boxy Fit customer could be sent the Ringer Tee's numbers/image."""
    boxy = call(ctx, "get_size_chart", product_id="boxy-wns-tee")
    ringer = call(ctx, "get_size_chart", product_id="ringer-tee")

    assert boxy["chart_id"] == "wns-boxy-tee"
    assert ringer["chart_id"] == "ringer-boxy-tee"
    assert boxy["chart_id"] != ringer["chart_id"]
    assert boxy["image"] != ringer["image"]
    assert boxy["sizes"] == {
        "S": {"width": 56, "length": 66},
        "M": {"width": 58, "length": 68},
        "L": {"width": 61, "length": 71},
        "XL": {"width": 63, "length": 73},
    }
    assert ringer["sizes"] == {
        "S": {"width": 54, "length": 65},
        "M": {"width": 56, "length": 67},
        "L": {"width": 58, "length": 69},
        "XL": {"width": 60, "length": 71},
    }


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
        # The tool has already had the confirmation sent, and says so: the
        # agent ends the turn there rather than writing a second one.
        "confirmation_sent",
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
    refused = call(ctx, "cancel_order", order_id=placed)
    assert refused["error"] == "already_shipped"
    assert refused["cancellable"] is False


# --- the exchange / cancellation terms (docs/policy.md) -------------------
#
# The numbers here are charged at the door in cash. Every one of them is
# asserted against `docs/policy.md`, not against what the tool happens to
# return, so a change to the terms has to be a deliberate edit in both places.


def _ship(ctx, order_id):
    order = ctx.session.get(Order, order_id)
    orders.advance_status(ctx.session, order, "Packed")
    orders.advance_status(ctx.session, order, "Shipped")
    ctx.session.flush()
    return order


def _deliver(ctx, order_id, *, hours_ago=0.0):
    order = _ship(ctx, order_id)
    orders.advance_status(ctx.session, order, "Delivered")
    order.delivered_at = utcnow() - timedelta(hours=hours_ago)
    ctx.session.flush()
    return order


def test_cancelling_before_shipping_costs_the_customer_nothing(ctx, placed):
    terms = call(ctx, "get_return_terms", order_id=placed)
    assert terms["cancellable"] is True
    assert terms["route"] == "cancel_now"
    assert terms["customer_pays"] == 0

    assert call(ctx, "cancel_order", order_id=placed)["status"] == "Cancelled"


def test_cancelling_after_shipping_is_a_return_and_costs_the_round_trip(ctx, placed):
    """The one the model must not work out for itself: refusing the parcel at
    the door is charged both ways. Quoting the 60 rather than the 120 is an
    argument at the door on a cash-on-delivery order."""
    _ship(ctx, placed)

    refused = call(ctx, "cancel_order", order_id=placed)
    assert refused["error"] == "already_shipped"
    assert refused["cancellable"] is False
    assert refused["route"] == "refuse_at_the_door"
    assert refused["shipping_fee"] == 60
    assert refused["customer_pays"] == 120, "one-way is the wrong number"
    assert refused["customer_pays_legs"] == {"delivery_attempt": 60, "return_trip": 60}

    # And it really did not cancel anything on the way past.
    assert ctx.session.get(Order, placed).status == "Shipped"


def test_a_shipped_order_cannot_report_the_exchange_window(ctx, placed):
    """`delivered_at` is stamped only when Shopify reports the delivery or
    staff press the button. A parcel that arrived and was never reported still
    reads Shipped, so the window is unknown -- never "closed"."""
    _ship(ctx, placed)
    terms = call(ctx, "get_return_terms", order_id=placed)
    assert terms["exchange_window"] == "unknown"
    assert terms["exchange_window_reason"] == "delivery_not_reported"
    assert "hours_since_delivery" not in terms


def test_an_exchange_inside_the_window_is_open(ctx, placed):
    _deliver(ctx, placed, hours_ago=3)
    terms = call(ctx, "get_return_terms", order_id=placed)
    assert terms["exchange_window"] == "open"
    assert terms["hours_since_delivery"] == 3.0
    assert terms["route"] == "exchange_within_window"


def test_an_exchange_after_twenty_four_hours_is_closed(ctx, placed):
    _deliver(ctx, placed, hours_ago=30)
    terms = call(ctx, "get_return_terms", order_id=placed)
    assert terms["exchange_window"] == "closed"
    assert terms["exchange_window_hours"] == 24


def test_a_defective_item_ships_at_the_shops_expense_and_a_swap_costs_twenty(ctx, placed):
    """Who pays is decided by *why*, and the model is given both answers
    rather than being trusted to remember which way round they go."""
    _deliver(ctx, placed, hours_ago=2)
    terms = call(ctx, "get_return_terms", order_id=placed)
    assert terms["defective_or_wrong_item"] == "shop_pays_shipping"
    assert terms["changed_mind"] == "customer_pays_shipping_plus_surcharge"
    assert terms["exchange_surcharge"] == 20
    # Regular shipping *plus* the surcharge -- not the surcharge on its own.
    assert terms["customer_pays"] == 80
    assert terms["exchange_condition"] == "original_packaging_unworn_clean"


def test_the_terms_answer_without_an_order_but_quote_no_amount(ctx):
    """"ممكن أستبدل؟" arrives before any order id does."""
    terms = call(ctx, "get_return_terms")
    assert terms["exchange_window_hours"] == 24
    assert terms["exchange_surcharge"] == 20
    assert terms["returns_accepted"] == "at_the_door_only"
    assert "customer_pays" not in terms and "shipping_fee" not in terms


def test_return_terms_are_scoped_to_the_asking_customer(ctx, placed, seeded):
    other = ToolContext(session=seeded, channel="whatsapp", external_id="201000000002")
    identities.client_for(seeded, "whatsapp", "201000000002")
    assert call(other, "get_return_terms", order_id=placed)["error"] == "order_not_found"


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
