"""A reply may not say it did what no tool did.

«ضفتهولك في السلة» beside an `out_of_stock` refusal, or beside no call at all;
«الأوردر اتسجل» beside a cart nobody checked out. The words are the model's
and the outcome is the tool layer's -- `assistant/action_claims.py` joins
them back together before the reply leaves.
"""

from __future__ import annotations

from assistant import action_claims, agent
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider

CHANNEL = "whatsapp"
WHO = "201000000666"
SOLD_OUT = "wanas-hoodie-m-grey"


def check(text: str, results=(), *, cart=False, orders=False) -> str:
    return action_claims.unbacked(
        text, list(results), cart_has_items=lambda: cart, has_orders=lambda: orders
    )


ADDED = ("add_to_cart", {"lines": [{"variant_id": "x"}], "subtotal": 650})
REFUSED = ("add_to_cart", {"error": "out_of_stock", "alternatives": []})


def test_an_add_the_tool_refused_is_not_an_add():
    assert check("تمام، ضفتهولك في السلة 👍", [REFUSED]) == "cart"
    assert check("اتضاف للسلة", []) == "cart"


def test_an_add_the_tool_made_is_fine():
    assert check("تمام، ضفتهولك في السلة 👍", [ADDED]) == ""


def test_an_offer_or_an_honest_no_is_not_a_claim():
    assert check("تحب أضيفه للسلة؟", []) == ""
    assert check("للأسف مقدرتش أضيفه، المقاس ده خلص.", [REFUSED]) == ""
    assert check("لسه مضفتوش، قولي المقاس الأول", []) == ""


def test_an_order_nobody_confirmed_is_not_placed():
    assert check("تمام، الأوردر اتسجل وهيوصلك خلال 4 أيام", [], cart=True) == "order"
    assert check("تم تأكيد الأوردر", [], cart=False, orders=False) == "order"
    failed = ("confirm_order", {"error": "items_out_of_stock", "items": []})
    assert check("الأوردر اتأكد", [failed], cart=True) == "order"


def test_a_status_about_an_existing_order_is_not_a_claim():
    status = ("get_my_orders", {"orders": [{"reference": "#1040", "status": "Confirmed"}]})
    assert check("أوردرك اتأكد ولسه ماتشحنش", [status], cart=True, orders=True) == ""
    # An empty cart and an order on file: the order that was placed.
    assert check("أوردرك اتسجل من شوية", [], cart=False, orders=True) == ""


def test_the_turn_is_sent_back_and_then_tells_the_truth(seeded):
    """The production shape: the size is sold out, the tool says so, and the
    reply said it was added anyway."""
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[{"id": "c1", "name": "add_to_cart", "arguments": {"variant_id": SOLD_OUT}}]
            ),
            ModelReply(text="تمام، ضفتهولك في السلة."),
            ModelReply(text="للأسف المقاس ده خلص في الرمادي، تحب الأسود؟"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "حطهولي", provider=provider)
    assert reply.text == "للأسف المقاس ده خلص في الرمادي، تحب الأسود؟"
    assert "add_to_cart" in provider.calls[-1][0].split("تنبيه داخلي")[-1]


def test_a_model_that_keeps_claiming_it_gets_the_true_sentence(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[{"id": "c1", "name": "add_to_cart", "arguments": {"variant_id": SOLD_OUT}}]
            )
        ]
        + [ModelReply(text="تمام، ضفتهولك في السلة.") for _ in range(3)]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "حطهولي", provider=provider)
    assert reply.text == action_claims.FALLBACKS["cart"]
    assert reply.error == "unbacked_cart_claim"
