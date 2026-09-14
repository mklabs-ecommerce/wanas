"""Egyptian, not Modern Standard: قميص is not a تيشيرت.

    customer: «فيه قمصان»
    bot:      «أيوه، عندنا تيشيرتات كتير:» + four t-shirts

In Egyptian a قميص is a button-up shirt -- collar, placket, buttons -- and
this shop does not sell one. The word was in the search vocabulary as
`"قميص": ("tee", "polo", "shirts")`, which is a Modern Standard Arabic
equivalence (there, قميص is the generic upper-body garment) applied to a
dialect that does not have it. The search then found t-shirts, the model read a
list of hits, and the shop said yes to something it does not stock.

Two rules come out of that and both are tested here:

* a garment we do not sell gets a plain "we don't have that", never the
  nearest thing renamed;
* a garment we *do* sell under a different Egyptian name has to be findable by
  that name -- which is the same failure with the sign flipped, and the reason
  «فانلة», «بلوفر» and «بنطرون» are in the synonym table now.
"""

from __future__ import annotations

import pytest

from assistant.tools.base import ToolContext, call_tool
from domain.services import garments, search_terms
from scripts import quality_gate

CHANNEL = "whatsapp"
WHO = "201000000001"


@pytest.fixture()
def ctx(seeded):
    return ToolContext(session=seeded, channel=CHANNEL, external_id=WHO)


# --- what the shop does not sell ------------------------------------------


@pytest.mark.parametrize(
    "word",
    ["قميص", "قمصان", "قميص أزرق", "عايز قميص", "shirt", "shirts", "button up"],
)
def test_a_shirt_is_recognised_as_something_we_do_not_sell(word):
    found = garments.not_sold(word)
    assert found is not None, word
    assert found[0] == "قميص"


@pytest.mark.parametrize(
    "word",
    ["تراكسوت", "تراك سوت", "شورت", "برمودا", "جينز", "جزمة", "شوز", "بدلة", "شنطة", "كاب"],
)
def test_the_rest_of_the_not_sold_vocabulary(word):
    assert garments.not_sold(word) is not None, word


@pytest.mark.parametrize(
    "word",
    ["تيشيرت", "تي شيرت", "فانلة", "هودي", "بولو", "بلوفر", "سويتر", "كنزة",
     "جاكيت", "بنطلون", "بنطال", "بنطرون", "تراك", "توب", "سويت شيرت"],
)
def test_what_we_do_sell_is_never_refused(word):
    assert garments.not_sold(word) is None, word


def test_our_own_category_names_are_not_read_as_shirts():
    """`T-Shirts` and `Polo Shirts` both end in the word this rule matches on.
    Reading either as "the customer asked for a button-up" would refuse two of
    the six things the shop actually sells."""
    for name in ("T-Shirts", "Polo Shirts", "polo shirt", "t shirt", "tee shirt"):
        assert garments.not_sold(name) is None, name


def test_a_set_is_not_its_two_halves():
    """The shop sells sweatpants and it sells sweatshirts. It does not sell
    them as a tracksuit, and «تراك» (a sweatpant, which we do sell) must not
    swallow «تراك سوت» (a set, which we do not)."""
    assert garments.not_sold("تراك") is None
    label, alternatives = garments.not_sold("عايز تراك سوت")
    assert label == "تراكسوت كامل"
    assert alternatives == ("Joggers & Sweatpants", "Hoodies & Sweatshirts")


def test_shoes_get_no_alternative_at_all():
    """There is no nearest thing to a pair of trainers in a shop that sells
    none, and offering a hoodie is the same sentence this module exists to
    stop."""
    assert garments.not_sold("جزمة")[1] == ()
    assert garments.not_sold("بدلة")[1] == ()


def test_no_word_is_in_both_halves_of_the_vocabulary():
    """A word that translates to a catalog token *and* refuses the search is a
    contradiction: whichever runs first decides, silently."""
    overlap = {
        word
        for word in garments.NOT_SOLD
        if search_terms.SYNONYMS.get(search_terms.normalize(word))
    }
    assert overlap == set(), overlap


# --- the tool answers it, rather than finding a near-enough match ----------


def test_asking_for_shirts_returns_a_refusal_not_a_list_of_tees(ctx):
    result = call_tool(ctx, "get_products", {"query": "قمصان"})
    assert result["error"] == "garment_not_sold"
    assert result["garment"] == "قميص"
    assert result["products"] == [], "nothing may be presented as the thing they asked for"
    assert result["alternatives"], "but something has to be offered"
    assert {p["category"] for p in result["alternatives"]} <= {"T-Shirts", "Polo Shirts"}


def test_the_refusal_sends_no_photo(ctx):
    """A picture of a t-shirt under "do you have shirts?" is the renaming this
    closes, in the one form the customer cannot miss."""
    call_tool(ctx, "get_products", {"query": "قمصان"})
    assert ctx.attachments == []


def test_the_category_argument_is_read_too(ctx):
    """The model passes the customer's word through whichever argument it
    thinks fits, and `category` is the other one."""
    assert call_tool(ctx, "get_products", {"category": "shirts"})["error"] == "garment_not_sold"


def test_a_garment_with_nothing_close_offers_nothing(ctx):
    result = call_tool(ctx, "get_products", {"query": "عايز جزمة"})
    assert result["error"] == "garment_not_sold"
    assert result["alternatives"] == []


def test_an_ordinary_search_is_untouched(ctx):
    result = call_tool(ctx, "get_products", {"query": "تيشيرت"})
    assert "error" not in result
    assert result["products"]


# --- and the names we do sell under still find them ------------------------


@pytest.mark.parametrize(
    ("word", "expected_category"),
    [
        ("فانلة", "T-Shirts"),
        ("فانيلة", "T-Shirts"),
        ("تي شيرت", "T-Shirts"),
        ("بلوفر", "Hoodies & Sweatshirts"),
        ("سويتر", "Hoodies & Sweatshirts"),
        ("كنزة", "Hoodies & Sweatshirts"),
        ("بنطال", "Joggers & Sweatpants"),
        ("بنطرون", "Joggers & Sweatpants"),
        ("جاكيت", "Jackets"),
    ],
)
def test_the_egyptian_name_finds_the_thing_on_the_shelf(ctx, word, expected_category):
    """The mirror-image failure. Every one of these is a garment this shop
    really sells, under a word a customer really types, and every one of them
    used to return nothing at all."""
    result = call_tool(ctx, "get_products", {"query": word})
    assert "error" not in result, word
    categories = {p["category"] for p in result["products"]}
    assert expected_category in categories, (word, sorted(categories))


# --- the gate rule ---------------------------------------------------------


def test_the_gate_fails_the_reply_that_started_this():
    assert quality_gate.offered_a_garment_they_did_not_ask_for(
        "فيه قمصان", "أيوه، عندنا تيشيرتات كتير: تيشيرت Ringer Tee و Envy T-shirt"
    )


def test_the_gate_fails_a_silent_substitution_too():
    """Not just the «أيوه». A reply that lists t-shirts without ever saying
    there are no shirts has renamed the garment just as squarely."""
    assert quality_gate.offered_a_garment_they_did_not_ask_for(
        "فيه قمصان", "عندنا تيشيرت Ringer Tee بـ 500 جنيه، تحب تشوفه؟"
    )


def test_the_gate_passes_the_honest_answer():
    assert (
        quality_gate.offered_a_garment_they_did_not_ask_for(
            "فيه قمصان",
            "معلش، مفيش قمصان عندنا خالص — بنبيع تيشيرتات وبولو بس. تحب أوريك التيشيرتات؟",
        )
        == ""
    )


def test_the_gate_ignores_a_question_about_something_we_sell():
    assert (
        quality_gate.offered_a_garment_they_did_not_ask_for(
            "فيه تيشيرتات؟", "أيوه، عندنا تيشيرتات كتير"
        )
        == ""
    )
