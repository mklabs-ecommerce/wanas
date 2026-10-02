"""A sizing question is answered from the chart, not handed to a person.

The demo-era prompt said there were no charts and sent every sizing question
to `size_help`; with Shopify charts in place the bot kept doing it. Every
sizing question now starts with get_size_chart, and `size_help` is refused for
a product that has a chart to answer from.
"""

from __future__ import annotations

import pytest

from assistant import agent
from assistant.prompt import SYSTEM_PROMPT
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from domain.models import Product, SizeChart
from domain.services import identities

USE_REAL_CATALOG = True

PRODUCT = "rehla-black-t-shirt"
WHO = "201000000777"


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
            fit={"recommended_weight_kg": {"S": [45, 55], "M": [55, 65]}},
            source="shopify",
        )
    )
    seeded.get(Product, PRODUCT).size_chart = "shopify-tee"
    seeded.commit()
    return seeded


def _turn(session, *calls_then_text):
    replies = [ModelReply(tool_calls=[c]) for c in calls_then_text[:-1]]
    replies.append(ModelReply(text=calls_then_text[-1]))
    provider = ScriptedProvider(replies)
    reply = agent.run_turn(session, "whatsapp", WHO, "المقاسات إيه؟", provider=provider)
    return reply, provider


def test_the_sizes_question_gets_numbers_and_no_handoff(charted):
    reply, provider = _turn(
        charted,
        {"id": "a", "name": "get_size_chart", "arguments": {"product_id": PRODUCT}},
        "مقاسات القطعة وهي مفرودة: S عرض 62 طول 70، M عرض 64 طول 73.",
    )
    assert "62" in reply.text and "73" in reply.text
    assert not identities.is_paused(charted, "whatsapp", WHO)


def test_size_help_is_refused_while_the_product_has_a_chart(charted):
    reply, _ = _turn(
        charted,
        {"id": "a", "name": "get_size_chart", "arguments": {"product_id": PRODUCT}},
        {"id": "b", "name": "request_human", "arguments": {"reason": "size_help", "summary": "size"}},
        "ده جدول القياسات: S عرض 62 طول 70، M عرض 64 طول 73.",
    )
    assert not identities.is_paused(charted, "whatsapp", WHO), "a charted product never pauses"


def test_the_chart_carries_the_published_weight_ranges(charted):
    from assistant.tools.base import ToolContext, call_tool

    ctx = ToolContext(session=charted, channel="whatsapp", external_id=WHO)
    result = call_tool(ctx, "get_size_chart", {"product_id": PRODUCT})
    assert result["has_chart"] is True
    assert result["sizes"]["S"] == {"width": 62, "length": 70}
    assert result["recommended_weight_kg"] == {"S": [45, 55], "M": [55, 65]}


def test_a_product_with_no_chart_still_hands_off(seeded):
    _turn(
        seeded,
        {"id": "a", "name": "get_size_chart", "arguments": {"product_id": "rehla-black-cap"}},
        {"id": "b", "name": "request_human", "arguments": {"reason": "size_help", "summary": "cap size"}},
        "",
    )
    assert identities.is_paused(seeded, "whatsapp", WHO)


def test_the_prompt_starts_every_sizing_question_with_the_chart():
    assert "سؤال عن القياسات أو أنهي مقاس يناسب يبدأ بـ get_size_chart" in SYSTEM_PROMPT
    # ...but «which sizes do you have» is stock, not fit: read as a chart
    # question it found no chart and handed a browsing customer to a person.
    assert "«متاح مقاسات إيه؟» بعد ما عرضت منتج = المتوفر، من get_variants" in SYSTEM_PROMPT
    assert "مفيش جدول مقاسات منشور" not in SYSTEM_PROMPT
    assert "recommended_weight_kg" in SYSTEM_PROMPT
