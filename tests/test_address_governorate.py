"""Reading the governorate out of an address the customer typed.

A customer wrote "شبين الكوم المنوفية شارع 9" -- the governorate is in the
sentence, in the middle of it -- and the bot answered with the region picker,
as though they had said nothing. Everything needed to avoid that already
existed: `shipping.resolve` knows the twenty-seven names, their Arabic labels
and the districts people use instead. What was missing was anyone *looking*.
`ask_governorate` offered a list without reading the message it was answering.

The rule the picker exists for does not change: the governorate is one of the
twenty-seven stored keys and free text never invents a new one. It only
selects one, by whole word, and the whole-word part is what keeps a street
called "شارع القناة" from being priced as Qena.
"""

from __future__ import annotations

import pytest

from assistant import messages as msg
from assistant.tools.base import ToolContext, call_tool, load_all
from domain.services import shipping

load_all()

WHO = "201555444333"


@pytest.fixture()
def ctx(seeded):
    return ToolContext(session=seeded, channel="whatsapp", external_id=WHO)


def _with(seeded, *customer_messages: str) -> ToolContext:
    """A context whose history is what the customer has just said."""
    return ToolContext(
        session=seeded,
        channel="whatsapp",
        external_id=WHO,
        history=[msg.user(text) for text in customer_messages],
    )


# --- reading the name out of a sentence -----------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "المنوفية شبين الكوم شارع 9",
        "شبين الكوم المنوفية شارع 9",
        "شارع 9، شبين الكوم، المنوفية",
        "انا ساكن في شارع ٩ متفرع من شارع الجيش، شبين الكوم، محافظة المنوفية",
        "عنواني: شارع 9 - شبين الكوم - المنوفيه",
    ],
)
def test_the_governorate_is_found_wherever_it_sits_in_the_sentence(seeded, address):
    assert shipping.detect(seeded, address) == ["Monufia"]


def test_a_district_names_its_governorate_just_as_well(seeded):
    """Nobody writes "القاهرة" when they mean Nasr City."""
    assert shipping.detect(seeded, "١٥ شارع مصطفى النحاس، مدينة نصر") == ["Cairo"]
    assert shipping.detect(seeded, "الحي الثاني، الشيخ زايد، فيلا 22") == ["Giza"]


def test_a_message_that_names_nowhere_finds_nothing(seeded):
    assert shipping.detect(seeded, "تمام كده؟") == []
    assert shipping.detect(seeded, "شارع 9، الدور التالت، شقة 4") == []


# --- and does not find it where it is not ---------------------------------


def test_a_street_that_merely_contains_a_name_is_not_that_governorate(seeded):
    """"قنا" sits inside "القناة". A plain substring test priced a street in
    Ismailia as a parcel to Qena -- 700km and a different fee."""
    assert shipping.detect(seeded, "شارع القناة، الإسماعيلية") == ["Ismailia"]
    assert shipping.resolve(seeded, "شارع القناة، الإسماعيلية") == "Ismailia"


def test_a_longer_name_wins_over_the_short_one_inside_it(seeded):
    assert shipping.detect(seeded, "مرسى مطروح") == ["Matrouh"]
    assert shipping.detect(seeded, "شرم الشيخ") == ["South Sinai"]


def test_two_names_for_one_place_are_one_answer(seeded):
    """The district and its governorate in the same line is the normal way to
    write an address, not an ambiguity."""
    assert shipping.detect(seeded, "طنطا الغربية") == ["Gharbia"]


def test_two_different_governorates_are_both_reported(seeded):
    found = shipping.detect(seeded, "انا من اسكندرية بس ابعتها لاختي في المنصورة")
    assert found == ["Alexandria", "Dakahlia"]


# --- what the tool does with that -----------------------------------------


def test_an_address_with_a_governorate_in_it_skips_the_picker(seeded):
    """The reported bug, at the seam it happened on."""
    ctx = _with(seeded, "شبين الكوم المنوفية شارع 9")

    result = call_tool(ctx, "ask_governorate", {})

    assert result["step"] == "done"
    assert result["governorate"] == "Monufia"
    assert ctx.interactive is None, "the customer was sent a list they had already answered"


def test_the_fee_follows_straight_from_what_was_read(seeded, cairo_rate):
    ctx = _with(seeded, "مدينة نصر، شارع مصطفى النحاس، عمارة 15")

    governorate = call_tool(ctx, "ask_governorate", {})["governorate"]
    assert call_tool(ctx, "get_shipping_fee", {"governorate": governorate}) == {
        "governorate": "Cairo",
        "fee": 60.0,
    }


def test_an_address_naming_nowhere_still_gets_the_regions(seeded):
    ctx = _with(seeded, "ابعتهالي على البيت", "شارع 9، الدور التالت")

    result = call_tool(ctx, "ask_governorate", {})

    assert result["step"] == "region"
    assert ctx.interactive["kind"] == "list"


def test_a_governorate_the_bot_said_is_not_the_customer_saying_it(seeded):
    """"Do you want it shipped to Cairo?" is a question, not an address."""
    ctx = ToolContext(
        session=seeded,
        channel="whatsapp",
        external_id=WHO,
        history=[msg.user("عايز اطلب"), msg.assistant("تحب نشحنها للقاهرة؟")],
    )

    assert call_tool(ctx, "ask_governorate", {})["step"] == "region"


def test_an_ambiguous_address_asks_rather_than_choosing(seeded):
    """Two real governorates in one message is the customer's call, but it is
    still a choice between two, not between twenty-seven."""
    ctx = _with(seeded, "انا من اسكندرية بس ابعتها لاختي في المنصورة")

    result = call_tool(ctx, "ask_governorate", {})

    assert result["step"] == "confirm"
    assert [row["governorate"] for row in result["governorates"]] == ["Alexandria", "Dakahlia"]
    assert len(ctx.interactive["sections"][0]["rows"]) == 2


def test_the_scan_does_not_reach_back_into_a_conversation_that_moved_on(seeded):
    """Only the last couple of things the customer said. A governorate
    mentioned six messages ago is not the address for this order."""
    ctx = _with(
        seeded,
        "اخر مرة طلبت وانا في اسوان",
        "المهم، عايز الهودي الاسود",
        "ابعتهولي",
    )

    assert call_tool(ctx, "ask_governorate", {})["step"] == "region"


def test_the_newest_message_is_the_one_that_counts(seeded):
    ctx = _with(seeded, "عايز اطلب", "طنطا، شارع البحر")

    assert call_tool(ctx, "ask_governorate", {})["governorate"] == "Gharbia"
