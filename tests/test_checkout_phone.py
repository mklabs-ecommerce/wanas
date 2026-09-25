"""The courier's phone number is one the customer gave, and one he can ring.

`confirm_order.contact_phone` is written by the model, and a phone number is
the one field of an order nobody can check by reading it: a transposed digit
looks exactly like the real number. It used to be checked for being non-blank
and nothing else. Two rules now, one per layer:

* `orders.place_order` accepts only an Egyptian mobile (010/011/012/015 and
  eight digits), in any of the ways people write one, and stores one
  canonical form;
* `confirm_order` accepts only a number the customer actually gave -- typed in
  this conversation, saved on their profile, or the WhatsApp number they are
  writing from.
"""

from __future__ import annotations

import pytest

from assistant.tools.base import ToolContext, call_tool
from domain.models import Order
from domain.services import orders

CHANNEL = "whatsapp"
WHO = "201000000444"
VARIANT = "wanas-hoodie-s-black"


@pytest.mark.parametrize(
    "written",
    ["01001234567", "+20 100 123 4567", "00201001234567", "201001234567", "1001234567",
     "٠١٠٠١٢٣٤٥٦٧", "0100-123-4567"],
)
def test_every_way_of_writing_one_mobile_is_the_same_mobile(written):
    assert orders.egyptian_mobile(written) == "01001234567"


@pytest.mark.parametrize(
    "written", ["0100123456", "010012345678", "0223456789", "01301234567", "hello", ""]
)
def test_what_a_courier_cannot_ring_is_not_a_mobile(written):
    assert orders.egyptian_mobile(written) is None


def checkout(session, *customer_messages: str) -> ToolContext:
    ctx = ToolContext(
        session=session,
        channel=CHANNEL,
        external_id=WHO,
        history=[{"role": "user", "content": text} for text in customer_messages],
    )
    call_tool(ctx, "add_to_cart", {"variant_id": VARIANT})
    return ctx


def confirm(ctx, phone: str) -> dict:
    return call_tool(
        ctx,
        "confirm_order",
        {
            "customer_name": "Omar",
            "governorate": "Cairo",
            "address": "12 Tahrir St, flat 3",
            "contact_phone": phone,
        },
    )


def test_a_number_the_customer_never_sent_is_refused(seeded, cairo_rate):
    """The model's own digits: one transposition away from the real number."""
    ctx = checkout(seeded, "رقمي 01001234567")
    result = confirm(ctx, "01001243567")
    assert result["error"] == "phone_not_given"
    assert seeded.query(Order).count() == 0


def test_the_number_they_typed_goes_through_in_one_canonical_form(seeded, cairo_rate):
    ctx = checkout(seeded, "رقمي ٠١٠٠ ١٢٣ ٤٥٦٧ لو سمحت")
    result = confirm(ctx, "+20 100 123 4567")
    assert result.get("order_id"), result
    assert seeded.get(Order, result["order_id"]).contact_phone == "01001234567"


def test_the_whatsapp_number_they_write_from_needs_no_retyping(seeded, cairo_rate):
    ctx = checkout(seeded, "نفس الرقم ده")
    result = confirm(ctx, "01000000444")
    assert result.get("order_id"), result


def test_a_landline_is_refused_as_a_number_the_courier_cannot_ring(seeded, cairo_rate):
    ctx = checkout(seeded, "رقمي 0223456789")
    assert confirm(ctx, "0223456789")["error"] == "invalid_phone"
