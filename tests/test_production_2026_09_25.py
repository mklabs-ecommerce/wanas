"""The conversation that proved the first fixes wrong, reproduced exactly.

Production, 2026-09-25 16:04-16:05 UTC, `whatsapp/201067177129`, deployed
commit 5f0a6a6 -- a staff member's own number, used for weeks of testing:

    16:04:11  POST .../whatsapp/201067177129/reset          (dashboard)
    16:04:23  customer: «السلام عليكم»
              bot:      «وعليكم السلام 🙂 معاك Wanas Gallery، تحب أساعدك في إيه؟»
              -> no name asked
    16:04:52  customer: «عايز اشوف السايز شارت بتاع boxy wns tee»
              tool get_products({'query': 'Boxy WNS Tee'})
              tool get_size_chart({'product_id': 'boxy-wns-tee'})
              showcase attached 2 photo(s) for boxy-wns-tee
              -> product photos on a size-chart question

1. **The name.** A reset is soft: it moves `context_start` and keeps the
   archive. `customer_name.already_asked` read the *whole* transcript, so a
   number whose history held any earlier «اسم حضرتك» (checkout asks it) was
   "already asked" forever -- the reset did not make it a new conversation.
   The tests only ever used numbers with no history.
2. **The chart.** «اشوف» was read as a request for photos, and the check for
   "is what they want to see the chart?" knew «جدول/مقاس/قياس» but not the
   loanwords «السايز شارت». So the message read as "photos *and* sizing" and
   both were kept. The tests only ever phrased it «جدول المقاسات».
"""

from __future__ import annotations

import pytest

from assistant import agent, customer_name, messages as msg, session as session_store
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from domain.services import conversation_reset, identities

pytestmark = pytest.mark.asks_name

CHANNEL = "whatsapp"
WHO = "201067177129"
GREETING = "السلام عليكم"
CHART_QUESTION = "عايz اشوف السايز شارت بتاع boxy wns tee".replace("z", "ز")
PRODUCTION_GREETING_REPLY = "وعليكم السلام 🙂 معاك Wanas Gallery، تحب أساعدك في إيه؟"


def _history_before_the_reset(seeded):
    """Weeks of testing from this number: among them a checkout that asked
    for the name, the way the prompt's order flow does."""
    session_store.save(
        seeded,
        CHANNEL,
        WHO,
        [
            msg.user("عايز Boxy WNS Tee مقاس L أسود"),
            msg.assistant("تمام، ضفتهولك. ممكن اسم حضرتك بالكامل عشان المندوب؟"),
            msg.user("حازم"),
            msg.assistant("تمام يا حازم."),
        ],
    )
    identities.get_or_create(seeded, CHANNEL, WHO)
    seeded.commit()


def _reset(seeded):
    session_store.clear(seeded, CHANNEL, WHO)  # what the registered clearer does
    conversation_reset.reset(seeded, CHANNEL, WHO, staff_id=1)
    seeded.commit()


def _charts(reply) -> list[str]:
    labels = reply.attachment_labels or {}
    return [
        p
        for p in reply.attachments
        if str((labels.get(p) or {}).get("label") or "").endswith("size chart")
    ]


def _garment_photos(reply) -> list[str]:
    charts = set(_charts(reply))
    return [p for p in reply.attachments if p not in charts]


# --------------------------------------------------------------------------
# 1. the name
# --------------------------------------------------------------------------


def test_after_a_reset_the_greeting_is_asked_for_a_name(seeded):
    _history_before_the_reset(seeded)
    _reset(seeded)

    provider = ScriptedProvider([ModelReply(text=PRODUCTION_GREETING_REPLY)])
    reply = agent.run_turn(seeded, CHANNEL, WHO, GREETING, provider=provider)

    # The turn was told to ask...
    assert "لسه منعرفش اسم الزبون" in provider.calls[0][0]
    # ...and even when the model answers exactly as production's did, the
    # customer is asked.
    assert customer_name.asks_for_name(reply.text)
    assert reply.text.startswith(PRODUCTION_GREETING_REPLY)


def test_a_reset_forgets_the_name_given_in_the_chat(seeded):
    identities.set_customer_name(seeded, CHANNEL, WHO, "حازم")
    seeded.commit()
    _reset(seeded)
    assert identities.known_name(seeded, CHANNEL, WHO) is None


def test_a_question_in_this_conversation_still_counts_as_asked(seeded):
    """Once per conversation, still: asked and ignored is answered."""
    history = [msg.user(GREETING), msg.assistant("أهلاً بحضرتك، ممكن أعرف اسم حضرتك؟")]
    session_store.save(seeded, CHANNEL, WHO, history)
    assert customer_name.decide(seeded, CHANNEL, WHO, history) == "asked"


def test_a_complaint_is_not_answered_with_a_name_question(seeded):
    provider = ScriptedProvider([ModelReply(text="آسفين جدًا على التأخير، هتابع الأوردر حالًا.")])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الاوردر اتأخر", provider=provider)
    assert not customer_name.asks_for_name(reply.text)


# --------------------------------------------------------------------------
# 2. the chart
# --------------------------------------------------------------------------


def test_the_chart_question_as_production_ran_it_sends_no_product_photo(seeded):
    """The exact tool sequence from the production log."""
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "p", "name": "get_products", "arguments": {"query": "Boxy WNS Tee"}}
                ]
            ),
            ModelReply(
                tool_calls=[
                    {"id": "c", "name": "get_size_chart", "arguments": {"product_id": "boxy-wns-tee"}}
                ]
            ),
            ModelReply(
                text="ده جدول مقاسات تيشيرت Boxy WNS Tee، والأرقام مقاسات القطعة وهي مفرودة مش مقاسات الجسم."
            ),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, CHART_QUESTION, provider=provider)
    assert _garment_photos(reply) == []
    assert _charts(reply), "the chart itself must still go"


def test_the_chart_question_answered_through_get_variants_sends_no_product_photo(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "v", "name": "get_variants", "arguments": {"product_id": "boxy-wns-tee"}}
                ]
            ),
            ModelReply(text="ده جدول مقاسات تيشيرت Boxy WNS Tee."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, CHART_QUESTION, provider=provider)
    assert _garment_photos(reply) == []


def test_a_sizing_question_never_carries_a_product_photo_even_without_a_chart(seeded):
    """«A size chart question must never get a product photo» -- whether or
    not a chart picture exists to send in its place."""
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "p", "name": "get_products", "arguments": {"query": "Boxy WNS Tee"}}
                ]
            ),
            ModelReply(text="تيشيرت Boxy WNS Tee متوفر من S لـ XL."),
        ]
    )
    reply = agent.run_turn(
        seeded, CHANNEL, WHO, "السايز شارت بتاع boxy wns tee ايه؟", provider=provider
    )
    assert _garment_photos(reply) == []


# --------------------------------------------------------------------------
# the words, one by one
# --------------------------------------------------------------------------


def _ctx(seeded, text):
    from assistant.tools.base import ToolContext

    return ToolContext(session=seeded, channel=CHANNEL, external_id=WHO, history=[msg.user(text)])


def test_reading_the_production_message(seeded):
    from assistant import showcase
    from assistant.tools.catalog_tools import asked_about_sizing

    ctx = _ctx(seeded, CHART_QUESTION)
    assert asked_about_sizing(ctx)
    assert not showcase.asked_for_photos(ctx)


def test_the_other_ways_of_asking_for_the_chart(seeded):
    from assistant import showcase

    for text in (
        "وريني السايز شارت",
        "ابعتلي صورة للجدول",
        "صورة السايز شارت لو سمحت",
        "عايز اشوف المقاسات",
        "show me the size chart",
    ):
        assert not showcase.asked_for_photos(_ctx(seeded, text)), text


def test_asking_for_both_is_still_both(seeded):
    from assistant import showcase

    for text in ("ابعتلي صور التيشيرت والجدول", "ابعتلي صور Boxy WNS Tee وجدول المقاسات"):
        assert showcase.asked_for_photos(_ctx(seeded, text)), text
