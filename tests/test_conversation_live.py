"""Conversation behaviour, against the real model.

Opt-in. These make real API calls and cost real quota, so they only run with:

    RUN_LIVE_TESTS=1 python -m pytest tests/test_conversation_live.py -v

They exist because the failures that prompted this work were failures of
*judgement* -- picking a t-shirt when asked for "something nice to go out in",
re-asking for a size the customer already gave. No test without a model can
catch that, and a test suite that only proves the plumbing works would have
gone green throughout the period the bot was behaving badly.

Assertions are deliberately tolerant: they check the shape of the behaviour
(did it ask rather than decide, did it stop re-asking, did it call the tool the
customer's words called for), never exact wording. A model that phrases things
differently is fine; a model that picks a product at random is not.
"""

from __future__ import annotations

import os

import pytest

from chatbot import agent
from chatbot.providers.gemini import GeminiProvider
from tests.conftest import REAL_LLM_KEY, REAL_LLM_MODEL

CHANNEL = "whatsapp"
WHO = "20100000live"

pytestmark = pytest.mark.skipif(
    not (REAL_LLM_KEY and os.getenv("RUN_LIVE_TESTS")),
    reason="live model tests: set RUN_LIVE_TESTS=1 and an LLM key",
)


@pytest.fixture()
def bot(seeded):
    """A conversation that keeps its history, like a real one."""
    provider = GeminiProvider(api_key=REAL_LLM_KEY, model=REAL_LLM_MODEL or "")
    turns: list[agent.AgentReply] = []

    def say(text: str) -> agent.AgentReply:
        reply = agent.run_turn(seeded, CHANNEL, WHO, text, provider=provider)
        seeded.commit()
        turns.append(reply)
        print(f"\n  user › {text}\n  bot  › {reply.text}\n  tools: {reply.tool_calls}")
        return reply

    say.turns = turns
    return say


def asks_something(text: str) -> bool:
    return "؟" in text or "?" in text


# --- A. A broad request must not be answered with one arbitrary product ---


@pytest.mark.parametrize(
    "opening",
    ["عايز حاجة حلوة للخروج", "وريني حاجة كاجوال", "عايز حاجة صيفي"],
)
def test_a_broad_request_gets_a_question_or_a_short_shortlist(bot, opening):
    reply = bot(opening)

    # It must not have decided for them.
    assert "add_to_cart" not in reply.tool_calls, "committed to a purchase from a vague request"

    # Either it asked something, or it offered a genuine choice -- not a single
    # product presented as the answer.
    offered_a_choice = reply.text.count("\n") >= 1 or "ولا" in reply.text
    assert asks_something(reply.text) or offered_a_choice, reply.text

    # And it must not have locked onto one product's variants straight away.
    assert reply.tool_calls.count("get_variants") <= 1, reply.tool_calls


def test_a_broad_request_does_not_dump_the_catalog(bot):
    reply = bot("عايز حاجة حلوة للخروج")
    # A shortlist, not eighteen products.
    assert reply.text.count("\n") <= 6, reply.text


# --- B / I. Everything supplied at once is not asked for again -------------


def test_a_fully_specified_request_is_not_re_interrogated(bot):
    reply = bot("عايز Cairokee T-shirt أسود XL واتنين")

    lowered = reply.text
    for already_given in ("اللون إيه", "أنهي لون", "المقاس إيه", "أنهي مقاس", "كام واحد", "الكمية"):
        assert already_given not in lowered, f"re-asked for something already given: {reply.text}"
    assert "get_variants" in reply.tool_calls


def test_only_the_missing_field_is_asked_for(bot):
    reply = bot("عايز الـ WANAS Hoodie الأسود")
    # Colour was given; size was not.
    assert asks_something(reply.text)
    assert "أنهي لون" not in reply.text and "اللون إيه" not in reply.text


# --- C / D / F / G. Context resolution -------------------------------------


def test_the_second_option_is_resolved_from_the_previous_turn(bot):
    bot("وريني تيشيرتات")
    reply = bot("التاني عجبني")
    # It must have acted on a product rather than asking what "التاني" means.
    assert reply.tool_calls, f"did not resolve 'التاني' to anything: {reply.text}"


def test_a_colour_change_keeps_the_current_product(bot):
    bot("عايز الـ WANAS Hoodie")
    reply = bot("طب الأسود")
    assert "hoodie" in reply.text.lower() or "get_variants" in reply.tool_calls
    # Adding to the cart here is fine and so is not adding; what must not
    # happen is the bot treating "طب الأسود" as a brand new request.


def test_a_reversal_is_a_change_not_a_new_conversation(bot):
    bot("عايز الـ WANAS Hoodie الزيتي")
    reply = bot("لا خلاص الأسود أحسن")
    # Must not respond as though it has never heard of the hoodie.
    assert "أنهي منتج" not in reply.text and "عايز إيه" not in reply.text, reply.text


def test_yes_is_read_against_the_question_that_was_just_asked(bot):
    first = bot("عايز الـ WANAS Hoodie الأسود")
    assert asks_something(first.text)
    reply = bot("أيوه")
    assert reply.text, "a bare 'أيوه' produced nothing"
    assert "مش فاهم" not in reply.text, reply.text


# --- E. Buying intent acts rather than re-confirming ----------------------


def test_explicit_purchase_intent_adds_to_the_cart(bot, seeded):
    bot("عايز Cairokee T-shirt أسود XL")
    reply = bot("خلاص حطهولي")

    from domain.services import carts

    cart = carts.cart_payload(seeded, CHANNEL, WHO)
    assert cart["item_count"] >= 1, f"purchase intent did not reach the cart: {reply.text}"


# --- H. Product images really arrive --------------------------------------


def test_asking_to_see_a_product_produces_a_real_attachment(bot):
    reply = bot("ممكن أشوف صور الـ WANAS Hoodie؟")
    assert reply.attachments, f"claimed to show a product but attached nothing: {reply.text}"
    assert all(p.startswith("data/") for p in reply.attachments)
    # And no path in the words.
    assert "data/" not in reply.text


# --- Tone -----------------------------------------------------------------


@pytest.mark.parametrize("phrase", ["هل ترغب في", "هل تريد أن", "يرجى", "وجدت المنتجات التالية"])
def test_the_banned_corporate_phrases_do_not_appear(bot, phrase):
    replies = [bot("عايز حاجة حلوة للخروج").text, bot("تيشيرت").text]
    for text in replies:
        assert phrase not in text, text
