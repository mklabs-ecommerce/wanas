"""One product reply, said once and in one piece.

From a production WhatsApp conversation about the `oversized plain t-shirt`:

    bot: available, Black/Navy/White, 300 -- «تحب لون إيه؟» -- «ممكن أعرف اسم حضرتك؟»
    bot: [photos]
    bot: «المقاس المتاح S بس» -- «تحب لون إيه؟ Black / Navy / White» again

Three separate faults: the name question bolted under an order question on a
customer who never greeted, the in-stock sizes missing from the search
result, and the same choice question asked twice in a row.
"""

from __future__ import annotations

import pytest

from assistant import customer_name, reply_rules
from domain.services import catalog

pytestmark = pytest.mark.asks_name

CHANNEL = "whatsapp"
WHO = "201000000078"


# -- the name ----------------------------------------------------------------


@pytest.mark.parametrize(
    "message", ["السلام عليكم", "السلام عليكم ورحمة الله", "هاي", "صباح الخير", "Hello", "ازيك يا باشا"]
)
def test_a_greeting_is_answered_with_the_name_question(seeded, message):
    assert customer_name.decide(seeded, CHANNEL, WHO, [], message) == "ask"


@pytest.mark.parametrize(
    "message", ["عايز تيشرت شبه ده ساده أوفر سايز", "السلام عليكم عايز هودي", "بكام البولو؟", ""]
)
def test_a_customer_who_opens_with_a_request_is_asked_at_checkout(seeded, message):
    assert customer_name.decide(seeded, CHANNEL, WHO, [], message) == "later"
    assert customer_name.turn_note(seeded, CHANNEL, WHO, [], message) == ""


def test_the_name_is_never_bolted_under_an_order_question():
    reply = "متوفر في Black و Navy و White بـ 300 جنيه. تحب لون إيه؟"
    assert customer_name.ensure_asked(reply, "ask") == reply
    assert customer_name.ensure_asked("أهلاً بحضرتك 👋", "ask").endswith(customer_name.ASK_LINE)
    greeting = "وعليكم السلام، تحب أساعدك في إيه؟"
    assert customer_name.ensure_asked(greeting, "ask").endswith(customer_name.ASK_LINE)


# -- the sizes ---------------------------------------------------------------


def test_a_search_says_which_sizes_are_for_sale(seeded):
    products = catalog.get_products(seeded, query="هودي")["products"]
    assert products
    for product in products:
        assert "in_stock_sizes" in product
        assert set(product["in_stock_sizes"]) <= set(product["sizes"])


# -- the repeated question ---------------------------------------------------

FIRST = "عندنا oversized plain t-shirt بـ 300 جنيه، ألوان Black و Navy و White. تحب لون إيه؟"


def test_a_colour_question_just_asked_is_not_asked_again():
    second = "المقاس المتاح S بس، تحب لون إيه؟"
    assert reply_rules.drop_repeated_choice_question(second, FIRST, "") == "المقاس المتاح S بس"


def test_a_different_question_is_kept():
    second = "متاح S و M. تحب مقاس إيه؟"
    assert reply_rules.drop_repeated_choice_question(second, FIRST, "") == second


def test_kept_when_the_customer_wrote_something():
    second = "الأسود والكحلي متاحين. تحب لون إيه؟"
    assert reply_rules.drop_repeated_choice_question(second, FIRST, "فيه ألوان تانية؟") == second


def test_never_leaves_nothing_to_send():
    assert reply_rules.drop_repeated_choice_question("تحب لون إيه؟", FIRST, "") == "تحب لون إيه؟"
