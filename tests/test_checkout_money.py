"""The total a customer agrees to is the total the courier collects.

Two numbers used to reach the pre-confirmation summary by routes the order
itself never took:

* the **cart** was priced from `variants.price`, a seeded wanas.db column,
  while `place_order` charged Shopify's live price -- so a price changed in
  Shopify Admin reached the order and not the summary the customer agreed to;
* the **total** was the model's own arithmetic: the cart subtotal plus the
  shipping fee, added up in its head and read out as "the real total".

Cash on delivery turns either one into an argument at the door. Both are code
now: the cart reads the live overlay, and `get_shipping_fee` returns the
summary with its sum already done (`checkout`).
"""

from __future__ import annotations

from decimal import Decimal

from assistant.tools.base import ToolContext, call_tool
from domain.models import Order

CHANNEL = "whatsapp"
WHO = "201000000555"

VARIANT = "wanas-hoodie-s-black"  # 650 in wanas.db


def ctx(session) -> ToolContext:
    return ToolContext(session=session, channel=CHANNEL, external_id=WHO)


def test_the_cart_is_priced_the_way_the_order_is_charged(seeded, shopify):
    shopify.set(VARIANT, price=720, compare=900)

    cart = call_tool(ctx(seeded), "add_to_cart", {"variant_id": VARIANT, "quantity": 2})

    line = cart["lines"][0]
    assert line["unit_price"] == 720, "the cart quoted wanas.db's price, not Shopify's"
    assert line["unit_original_price"] == 900
    assert line["line_total"] == 1440
    assert cart["subtotal"] == 1440


def test_the_checkout_total_is_worked_out_in_code(seeded, cairo_rate):
    tools = ctx(seeded)
    call_tool(tools, "add_to_cart", {"variant_id": VARIANT, "quantity": 2})
    call_tool(tools, "add_to_cart", {"variant_id": "ringer-tee-xl-burgundy"})

    fee = call_tool(tools, "get_shipping_fee", {"governorate": "Cairo"})

    checkout = fee["checkout"]
    assert checkout["subtotal"] == 2 * 650 + 500
    assert checkout["shipping_fee"] == 60
    assert checkout["total"] == 2 * 650 + 500 + 60
    assert checkout["item_count"] == 3
    assert [line["quantity"] for line in checkout["lines"]] == [2, 1]


def test_an_empty_cart_gets_only_the_fee(seeded, cairo_rate):
    fee = call_tool(ctx(seeded), "get_shipping_fee", {"governorate": "Cairo"})
    assert fee == {"governorate": "Cairo", "fee": 60}


def test_the_total_quoted_before_confirming_is_the_order_total(seeded, cairo_rate, shopify):
    """End to end, with Shopify disagreeing with wanas.db: the number in the
    summary and the number on the order are the same number."""
    shopify.set(VARIANT, price=720)
    tools = ctx(seeded)
    call_tool(tools, "add_to_cart", {"variant_id": VARIANT})
    quoted = call_tool(tools, "get_shipping_fee", {"governorate": "Cairo"})["checkout"]["total"]

    placed = call_tool(
        tools,
        "confirm_order",
        {
            "customer_name": "Omar",
            "governorate": "Cairo",
            "address": "12 Tahrir St, flat 3",
            "contact_phone": "01000000555",  # the WhatsApp number they write from
        },
    )
    order = seeded.get(Order, placed["order_id"])
    assert Decimal(str(quoted)) == order.total == Decimal("780")
