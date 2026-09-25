"""A price, a total or a measurement in a reply is one a tool returned.

The prompt has always said so; nothing checked. These pin the check
(`assistant/reply_facts.py`): what counts as a stated amount, what counts as
knowing it, and what the turn does with a number nobody looked up.
"""

from __future__ import annotations

from decimal import Decimal

from assistant import agent, reply_facts
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider

CHANNEL = "whatsapp"
WHO = "201000000888"

D = Decimal


def money(text: str) -> list[Decimal]:
    return reply_facts.stated(text)[0]


def cm(text: str) -> list[Decimal]:
    return reply_facts.stated(text)[1]


# --- what a reply states --------------------------------------------------------


def test_an_amount_beside_the_currency_is_money():
    assert money("تيشيرت Ringer Tee — السعر 580 جنيه") == [D(580)]
    assert money("• الإجمالي — 1,189 جنيه كاش عند الاستلام") == [D(1189)]
    assert money("السعر من ٥٠٠ لـ ٥٨٠ جنيه") == [D(500), D(580)]


def test_an_amount_after_a_price_word_is_money_without_the_currency():
    assert money("الهودي بـ 650 بس") == [D(650)]


def test_quantities_days_references_and_phones_are_not_money():
    assert money("2 قطع بـ 1300 جنيه") == [D(1300)]
    assert money("التوصيل من 2 لـ 4 أيام والشحن 110 جنيه") == [D(110)]
    assert money("رقم الأوردر #1040 والإجمالي 780 جنيه") == [D(780)]
    assert money("هنكلمك على 01001234567 والإجمالي 780 جنيه") == [D(780)]
    assert money("عليه خصم 30% النهارده") == []


def test_centimetres_are_measurements():
    assert cm("• مقاس L — عرض 61 سم، طول 71 سم") == [D(61), D(71)]
    assert money("• مقاس L — عرض 61 سم، طول 71 سم") == []


# --- what counts as knowing it -------------------------------------------------


def history_with(content: dict, customer: str = "بكام؟") -> list[dict]:
    return [
        {"role": "user", "content": customer},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "get_variants", "arguments": {}}],
        },
        {"role": "tool_results", "results": [{"id": "c1", "name": "get_variants", "content": content}]},
    ]


def test_a_number_a_tool_returned_is_known():
    history = history_with({"variants": [{"price": 580.0, "original_price": 580.0}]})
    assert reply_facts.ungrounded("السعر 580 جنيه", history) == []


def test_a_number_nobody_returned_is_not():
    history = history_with({"variants": [{"price": 580.0}]})
    assert reply_facts.ungrounded("السعر 450 جنيه", history) == ["450 جنيه"]
    assert reply_facts.ungrounded("عرض 64 سم", history) == ["64 سم"]


def test_the_customers_own_number_may_be_repeated():
    history = history_with({"variants": []}, customer="معايا 700 جنيه بس")
    assert reply_facts.ungrounded("تمام، في حدود 700 جنيه عندنا اختيارات", history) == []


def test_the_shops_published_amounts_are_known(seeded, cairo_rate):
    constants = reply_facts.shop_constants(seeded)
    assert reply_facts.ungrounded("الشحن 60 جنيه", [], constants) == []
    assert reply_facts.ungrounded("الاستبدال بزيادة 20 جنيه", [], constants) == []
    assert reply_facts.ungrounded("رفض الشحنة بيكلف 120 جنيه", [], constants) == []


# --- what the turn does -------------------------------------------------------------


def lookup(product_id: str = "ringer-tee") -> ModelReply:
    return ModelReply(
        tool_calls=[{"id": "c1", "name": "get_variants", "arguments": {"product_id": product_id}}]
    )


def test_a_price_nobody_looked_up_is_sent_back(seeded):
    """The Ringer tee is 580 (500 in burgundy XL). A reply quoting 450 from
    memory used to reach the customer as it was."""
    provider = ScriptedProvider(
        [
            lookup(),
            ModelReply(text="الـ Ringer Tee بـ 450 جنيه."),
            ModelReply(text="الـ Ringer Tee بـ 580 جنيه، والـ XL البرجندي بـ 500 جنيه."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الرينجر بكام؟", provider=provider)

    assert "450" not in reply.text
    assert "580" in reply.text
    nudged = provider.calls[-1][0]
    assert "450 جنيه" in nudged.split("تنبيه داخلي")[-1]


def test_a_model_that_will_not_stop_gets_the_fallback_question(seeded):
    provider = ScriptedProvider(
        [lookup()] + [ModelReply(text="الـ Ringer Tee بـ 450 جنيه.") for _ in range(3)]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الرينجر بكام؟", provider=provider)
    assert reply.error == "ungrounded_numbers"
    assert "450" not in reply.text


def test_a_total_the_model_added_up_itself_is_sent_back(seeded, cairo_rate):
    """The subtotal and the fee are each known; their sum is not, until
    `get_shipping_fee`'s `checkout` says it."""
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "add_to_cart",
                        "arguments": {"variant_id": "wanas-hoodie-s-black"},
                    }
                ]
            ),
            ModelReply(text="ضفته. الإجمالي 710 جنيه مع الشحن."),
            ModelReply(
                tool_calls=[
                    {"id": "c2", "name": "get_shipping_fee", "arguments": {"governorate": "Cairo"}}
                ]
            ),
            ModelReply(text="ضفته. الإجمالي 710 جنيه مع الشحن للقاهرة."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ضيفه وأنا من القاهرة", provider=provider)
    assert reply.error is None
    assert "710" in reply.text
    assert "get_shipping_fee" in reply.tool_calls


def test_measurements_carry_the_garment_flat_caveat():
    text = "مقاس L — عرض 61 سم، طول 71 سم"
    assert reply_facts.with_flat_note(text).endswith(reply_facts.FLAT_NOTE)
    already = "دي مقاسات القطعة وهي مفرودة: مقاس L — عرض 61 سم"
    assert reply_facts.with_flat_note(already) == already
    assert reply_facts.with_flat_note("السعر 580 جنيه") == "السعر 580 جنيه"
