"""«فين جدول المقاسات» looped get_size_chart -> request_human -> refused.

Replayed from a real conversation (2026-10-01): a chart imported from Shopify
with numbers and no picture, a model that reached for `size_help` three times
over, a reply saying "the chart arrived" with no chart in it, then a fallback
asking which product -- and finally "this is a picture of" beside no picture.
"""

from __future__ import annotations

import pytest

from assistant import agent
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from domain.models import Product, SizeChart
from domain.services import identities

USE_REAL_CATALOG = True
PRODUCT = "rehla-black-t-shirt"
WHO = "201000000778"
CHART = {"id": "a", "name": "get_size_chart", "arguments": {}}
HUMAN = {"id": "b", "name": "request_human", "arguments": {"reason": "size_help", "summary": "size"}}
VARIANTS = {"id": "v", "name": "get_variants", "arguments": {"product_id": PRODUCT}}


@pytest.fixture()
def charted(seeded):
    seeded.add(
        SizeChart(
            chart_id="shopify-tee",
            title="T-shirt Size Chart",
            unit="cm",
            measurements=[
                {"key": "width", "label_en": "Width", "label_ar": "العرض", "marker": "A"},
                {"key": "length", "label_en": "Length", "label_ar": "الطول", "marker": "B"},
            ],
            sizes={"S": {"width": 62, "length": 70}, "M": {"width": 64, "length": 73}},
            source="shopify",
        )
    )
    seeded.get(Product, PRODUCT).size_chart = "shopify-tee"
    seeded.commit()
    return seeded


def _turn(session, text, *steps):
    replies = [ModelReply(tool_calls=[s]) if isinstance(s, dict) else ModelReply(text=s) for s in steps]
    return agent.run_turn(session, "whatsapp", WHO, text, provider=ScriptedProvider(replies))


def _about_the_black_tee(session):
    _turn(session, "عايزة التيشيرت الاسود", VARIANTS, "ده Rehla Black T-Shirt، متاح S وM وL.")


def _has_the_chart(text):
    return all(n in text for n in ("62", "70", "64", "73"))


def test_the_production_loop_ends_with_the_chart_in_the_reply(charted):
    _about_the_black_tee(charted)
    reply = _turn(
        charted,
        "فين جدول المقاسات",
        CHART, HUMAN, CHART, HUMAN, CHART,
        "جدول المقاسات للتيشيرت الأسود وصل، دي مقاسات القطعة مش الجسم.",
    )
    assert _has_the_chart(reply.text), reply.text
    assert "Rehla Black T-Shirt" in reply.text, "resolved from context, never asked for"
    assert not identities.is_paused(charted, "whatsapp", WHO)


def test_a_reply_that_leaves_the_numbers_out_gets_them(charted):
    _about_the_black_tee(charted)
    reply = _turn(charted, "فين جدول المقاسات", CHART, "جدول المقاسات وصل. قوليلي وزنك وأساعدك.")
    assert _has_the_chart(reply.text)
    assert reply.text.index("62") < reply.text.index("وزنك"), "the chart first, the offer after"


def test_a_reply_that_quotes_the_chart_is_left_alone(charted):
    _about_the_black_tee(charted)
    said = "مقاسات القطعة وهي مفرودة: S عرض 62 طول 70، M عرض 64 طول 73."
    reply = _turn(charted, "المقاسات إيه؟", CHART, said)
    assert reply.text.count("62") == 1


def test_asking_for_the_chart_again_gets_it_again(charted):
    _about_the_black_tee(charted)
    _turn(charted, "فين جدول المقاسات", CHART, "اهو الجدول.")
    reply = _turn(charted, "قولي انت جدول المقاسات", CHART, HUMAN, CHART, "اتفضلي.")
    assert _has_the_chart(reply.text)
    assert not identities.is_paused(charted, "whatsapp", WHO)


def test_the_size_help_refusal_carries_the_chart_itself(charted):
    from assistant.tools.base import ToolContext, call_tool

    _about_the_black_tee(charted)
    from assistant import session as session_store

    history = session_store.load(charted, "whatsapp", WHO)
    ctx = ToolContext(session=charted, channel="whatsapp", external_id=WHO, history=history)
    refused = call_tool(ctx, "request_human", HUMAN["arguments"])
    assert refused["error"] == "size_chart_available"
    assert "62" in refused["chart_table"] and "Do not call request_human" in refused["detail"]


def test_an_identical_call_is_not_run_a_third_time(charted):
    reply = _turn(charted, "التيشيرت الاسود", VARIANTS, VARIANTS, VARIANTS, VARIANTS, "ده هو.")
    assert reply.tool_calls.count("get_variants") == 4
    assert reply.error == "repeated_calls"


def test_a_refused_call_is_not_run_twice(seeded):
    calls = []
    original = agent.call_tool

    def spy(ctx, name, arguments):
        calls.append(name)
        return original(ctx, name, arguments)

    agent.call_tool = spy
    try:
        bad = {"id": "x", "name": "get_variants", "arguments": {"product_id": "no-such-tee"}}
        _turn(seeded, "التيشيرت", bad, bad, "قوليلي اسم المنتج.")
    finally:
        agent.call_tool = original
    assert calls == ["get_variants"]


def test_a_genuine_handoff_still_hands_off(charted):
    ask = {"id": "h", "name": "request_human", "arguments": {"reason": "customer_asked", "summary": "wants a person"}}
    reply = _turn(charted, "عايزة أكلم حد", ask, "")
    assert identities.is_paused(charted, "whatsapp", WHO)
    assert reply.error is None


def test_no_picture_is_claimed_that_did_not_go(charted):
    _about_the_black_tee(charted)  # its photo went out here, so it is not sent again
    reply = _turn(
        charted,
        "Rehla Black T-Shirt",
        VARIANTS,
        "اتفضلي دي صورة Rehla Black T-Shirt، بـ 600 بدل 750.",
        "Rehla Black T-Shirt بـ 600 بدل 750، تحبي مقاس إيه؟",
    )
    assert not reply.attachments
    assert "صورة" not in reply.text, reply.text
    assert "600" in reply.text


def test_the_image_fallback_names_the_product_already_discussed(charted):
    from assistant import session as session_store

    _about_the_black_tee(charted)
    history = session_store.load(charted, "whatsapp", WHO)
    assert "Rehla Black T-Shirt" in agent.image_promise_fallback(history)
    assert agent.image_promise_fallback([]) == agent.IMAGE_PROMISE_FALLBACK
