"""The quality gate's reply rules, held to on every live reply.

Each rule was written after a real conversation went wrong and then ran only
offline, against a benchmark. `assistant/reply_rules.py` runs them on the
reply about to leave: `correct` fixes what has one right answer, and
`violation` sends the turn back for what only a new sentence can fix.
"""

from __future__ import annotations

from assistant import agent, reply_rules, session as session_store
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider

CHANNEL = "whatsapp"
WHO = "201000000777"

VOCABULARY = ["Lightweight", "Sweatpant", "Ringer", "WANAS", "Hoodie", "Olive"]


def fixed(text: str, references=None, money=False) -> str:
    return reply_rules.correct(
        text, vocabulary=VOCABULARY, references=references or {}, states_money=money
    )[0]


# --- one right answer: corrected in place ------------------------------------------


def test_a_mangled_product_name_is_put_back():
    """«Lightwelson Sweatpant», six times in one real conversation."""
    assert fixed("ده Lightwelson Sweatpant الأسود") == "ده Lightweight Sweatpant الأسود"


def test_an_internal_order_id_becomes_the_customers_reference():
    assert fixed("أوردرك WNS-12 اتأكد", {"WNS-12": "#1040"}) == "أوردرك #1040 اتأكد"


def test_the_shops_name_is_spelled_its_one_way():
    assert fixed("أهلاً بيك في Wanass") == "أهلاً بيك في Wanas"
    assert fixed("تيشيرت Boxy WNS Tee") == "تيشيرت Boxy WNS Tee", "a product name is not the brand"


def test_an_identifier_is_never_rewritten():
    assert fixed("[wanas-hoodie-s-black]") == "[wanas-hoodie-s-black]"


def test_one_emoji_at_most_and_none_beside_a_price_or_an_apology():
    assert fixed("صباح النور 🙂 تحب أساعدك في إيه؟ 👍") == "صباح النور 🙂 تحب أساعدك في إيه؟"
    assert fixed("ده بـ 650 جنيه 👆", money=True) == "ده بـ 650 جنيه"
    assert fixed("معلش 🙏 المشكلة عندنا") == "معلش المشكلة عندنا"


def test_ordinary_english_is_left_alone():
    assert fixed("المقاسات small و medium و large") == "المقاسات small و medium و large"


# --- only a new sentence fixes it: the turn is sent back -----------------------


def rule(text: str, *, customer: str = "", previous: str = "", results=()) -> str:
    return reply_rules.violation(text, customer=customer, previous=previous, results=list(results))


def test_a_payment_method_the_shop_cannot_take():
    assert rule("تقدر تدفع بانستاباي كمان").startswith("payment")
    assert rule("مش بنقبل فيزا، كاش عند الاستلام بس") == ""


def test_a_whole_line_denied_without_looking():
    assert rule("مفيش قسم حريمي عندنا").startswith("section")
    looked = [("get_products", {"products": [], "count": 0})]
    assert rule("مفيش قسم حريمي عندنا", results=looked) == ""


def test_a_garment_we_do_not_sell_is_never_answered_yes():
    refused = [("get_products", {"error": "garment_not_sold", "garment": "قمصان", "alternatives": []})]
    assert rule("أيوه عندنا تيشيرتات كتير", customer="فيه قمصان؟", results=refused).startswith("garment")
    assert rule("مفيش قمصان عندنا، بس فيه تيشيرتات", customer="فيه قمصان؟", results=refused) == ""
    assert rule("أيوه عندنا تيشيرتات كتير", customer="فيه قمصان؟").startswith("garment")


def test_a_sleeve_length_is_never_professed_unknown():
    assert rule("للأسف مش عارف طول الكم بتاعه").startswith("sleeve")


def test_the_previous_reply_is_not_sent_again():
    before = "عندنا Lightweight Sweatpant و WANAS Sweatpant، تحب تشوف صور ولا مقاسات؟"
    assert rule(before, previous=before).startswith("repeat")
    assert rule("تحب أساعدك في إيه؟", previous="تحب أساعدك في إيه؟") == "", "too short to matter"


# --- in a turn -------------------------------------------------------------------------


def test_the_turn_sends_the_corrected_name(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "c1", "name": "get_variants", "arguments": {"product_id": "lightweight-sweatpant"}}
                ]
            ),
            ModelReply(text="الـ Lightwelson Sweatpant متاح في الأسود."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "البنطلون الخفيف متاح؟", provider=provider)
    assert "Lightweight Sweatpant" in reply.text
    assert "Lightwelson" not in reply.text


def test_the_turn_is_sent_back_for_a_payment_method_it_cannot_take(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(text="تقدر تدفع بفودافون كاش أو انستاباي."),
            ModelReply(text="بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الدفع إزاي؟", provider=provider)
    assert reply.text == "بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع."
    assert "طريقة دفع" in provider.calls[-1][0].split("تنبيه داخلي")[-1]


def test_the_turn_is_sent_back_for_saying_the_same_thing_again(seeded):
    before = "عندنا Lightweight Sweatpant و WANAS Sweatpant، تحب تشوف صور ولا مقاسات؟"
    session_store.save(
        seeded,
        CHANNEL,
        WHO,
        [{"role": "user", "content": "عندكم بناطيل؟"}, {"role": "assistant", "content": before}],
    )
    provider = ScriptedProvider(
        [ModelReply(text=before), ModelReply(text="تمام، تحب مقاس إيه؟")]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الاتنين", provider=provider)
    assert reply.text == "تمام، تحب مقاس إيه؟"
