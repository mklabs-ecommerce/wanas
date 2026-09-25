"""A size-chart question gets the chart; a product question gets the photos.

Reported: «when I ask for the size chart, it sends the size chart together
with product photos». It did, by design: `get_variants` attached the
product's photo on every call and, when the customer's message was about
sizing, the chart beside it -- "so an answer listing sizes always arrives with
the chart beside it" -- and a single-hit `get_products` did the same through
the other door. The showcase could then add the product's other colourways on
top of the chart, since it only stood down when `get_size_chart` was the sole
catalog call of the turn.

The rule now, decided from the customer's own words and applied to the
pictures that are actually leaving, whichever tools produced them:

* a reply carrying a size chart carries no garment photo,
* a reply carrying garment photos carries no chart (that half already held),
* unless the customer's message asked for both.
"""

from __future__ import annotations

import pytest

from assistant import agent, messages as msg, showcase
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.tools.base import ToolContext

CHANNEL = "whatsapp"
PRODUCT = "boxy-wns-tee"


def _call(name: str, **arguments) -> ModelReply:
    return ModelReply(tool_calls=[{"id": f"c-{name}", "name": name, "arguments": arguments}])


def _turn(seeded, who: str, text: str, *script: ModelReply):
    return agent.run_turn(seeded, CHANNEL, who, text, provider=ScriptedProvider(list(script)))


def _charts(reply) -> list[str]:
    labels = reply.attachment_labels or {}
    return [
        path
        for path in reply.attachments
        if str((labels.get(path) or {}).get("label") or "").endswith("size chart")
    ]


def _photos(reply) -> list[str]:
    charts = set(_charts(reply))
    return [path for path in reply.attachments if path not in charts]


# --------------------------------------------------------------------------
# the reported behaviour, reproduced -- every one of these mixed the two
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "ابعتلي جدول المقاسات بتاع Boxy WNS Tee",
        "مقاسات Boxy WNS Tee ايه؟",
        "size chart for Boxy WNS Tee",
    ],
)
def test_a_chart_question_answered_through_get_variants_sends_the_chart_only(seeded, question):
    reply = _turn(
        seeded,
        "201000000101",
        question,
        _call("get_variants", product_id=PRODUCT),
        ModelReply(text="ده جدول مقاسات تيشيرت Boxy WNS Tee، والأرقام مقاسات القطعة وهي مفرودة."),
    )
    assert _charts(reply), "the chart must still go"
    assert _photos(reply) == []


def test_a_chart_question_answered_by_both_tools_sends_the_chart_only(seeded):
    reply = _turn(
        seeded,
        "201000000102",
        "عايز جدول المقاسات بتاع Boxy WNS Tee",
        ModelReply(
            tool_calls=[
                {"id": "a", "name": "get_size_chart", "arguments": {"product_id": PRODUCT}},
                {"id": "b", "name": "get_variants", "arguments": {"product_id": PRODUCT}},
            ]
        ),
        ModelReply(text="ده جدول مقاسات تيشيرت Boxy WNS Tee."),
    )
    assert len(_charts(reply)) == 1
    assert _photos(reply) == []


def test_a_chart_question_answered_by_a_search_sends_the_chart_only(seeded):
    reply = _turn(
        seeded,
        "201000000103",
        "جدول مقاسات Boxy WNS Tee",
        _call("get_products", query="Boxy WNS Tee"),
        ModelReply(text="ده جدول مقاسات تيشيرت Boxy WNS Tee."),
    )
    assert _charts(reply)
    assert _photos(reply) == []


def test_the_showcase_does_not_top_a_chart_up_with_photos(seeded):
    """The chart came from get_variants, the reply names the product: the
    showcase used to read that as a first showing and add colourways."""
    reply = _turn(
        seeded,
        "201000000104",
        "المقاسات ايه في Boxy WNS Tee؟",
        _call("get_variants", product_id=PRODUCT),
        ModelReply(text="تيشيرت Boxy WNS Tee متوفر من S لـ XL، وده جدول المقاسات."),
    )
    assert _charts(reply)
    assert _photos(reply) == []


# --------------------------------------------------------------------------
# what must keep working
# --------------------------------------------------------------------------


def test_a_chart_asked_for_through_get_size_chart_is_still_the_chart_only(seeded):
    reply = _turn(
        seeded,
        "201000000105",
        "جدول المقاسات بتاع Boxy WNS Tee",
        _call("get_size_chart", product_id=PRODUCT),
        ModelReply(text="ده جدول مقاسات تيشيرت Boxy WNS Tee."),
    )
    assert len(_charts(reply)) == 1
    assert _photos(reply) == []


def test_a_product_question_sends_photos_and_no_chart(seeded):
    reply = _turn(
        seeded,
        "201000000106",
        "عايز أشوف Boxy WNS Tee",
        _call("get_variants", product_id=PRODUCT),
        ModelReply(text="تيشيرت Boxy WNS Tee، السعر 590 جنيه."),
    )
    assert _photos(reply)
    assert _charts(reply) == []


def test_a_customer_who_asks_for_both_gets_both(seeded):
    reply = _turn(
        seeded,
        "201000000107",
        "ابعتلي صور Boxy WNS Tee وجدول المقاسات",
        _call("get_variants", product_id=PRODUCT),
        ModelReply(text="دي صورة تيشيرت Boxy WNS Tee، وده جدول المقاسات."),
    )
    assert _photos(reply)
    assert _charts(reply)


def test_the_chart_is_not_counted_as_already_sent_when_it_was_withheld(seeded):
    """A photo dropped from a chart reply was never sent -- asking to see the
    product next is answered with the photo, not refused as a repeat."""
    who = "201000000108"
    _turn(
        seeded,
        who,
        "جدول مقاسات Boxy WNS Tee",
        _call("get_variants", product_id=PRODUCT),
        ModelReply(text="ده جدول مقاسات تيشيرت Boxy WNS Tee."),
    )
    reply = _turn(
        seeded,
        who,
        "طب ابعتلي صورته",
        _call("get_variants", product_id=PRODUCT),
        ModelReply(text="دي صورة تيشيرت Boxy WNS Tee."),
    )
    assert _photos(reply)
    assert _charts(reply) == []


# --------------------------------------------------------------------------
# reading the customer's words
# --------------------------------------------------------------------------


def _ctx(seeded, text: str) -> ToolContext:
    return ToolContext(session=seeded, channel=CHANNEL, external_id="x", history=[msg.user(text)])


@pytest.mark.parametrize(
    "text",
    ["ابعتلي صوره", "عايز أشوف Boxy WNS Tee", "وريني الأسود", "send me a photo", "ابعتلي كل الألوان"],
)
def test_a_request_to_see_the_garment(seeded, text):
    assert showcase.asked_for_photos(_ctx(seeded, text))


@pytest.mark.parametrize(
    "text",
    [
        "ابعتلي صورة جدول المقاسات",
        "صورة المقاسات لو سمحت",
        "picture of the size chart",
        "المقاسات ايه؟",
        "بكام؟",
    ],
)
def test_a_picture_of_the_chart_is_not_a_request_for_photos(seeded, text):
    assert not showcase.asked_for_photos(_ctx(seeded, text))
