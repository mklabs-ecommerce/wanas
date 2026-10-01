"""Searching the catalog the way a customer actually types.

The catalog is entirely in English. Before `search_terms` existed,
`get_products(query="هودي أسود")` returned nothing at all -- the bot only
appeared to work because the model translated on its own before calling the
tool. These tests are the guarantee that replaced that habit.
"""

from __future__ import annotations

import pytest

from domain.services import catalog
from domain.services.search_terms import matches, normalize, query_tokens

USE_REAL_CATALOG = True


# --- normalisation --------------------------------------------------------


@pytest.mark.parametrize(
    "written,same_as",
    [
        ("هودى", "هودي"),        # alef maqsura for ya
        ("أسود", "اسود"),        # hamza
        ("إسكندرية", "اسكندريه"),  # hamza + ta marbuta
        ("زيتــي", "زيتي"),       # tatweel
        ("أَسْوَد", "اسود"),        # diacritics
        ("Quarter-Zip", "quarter zip"),
        ("٢٠٢٤", "2024"),         # arabic-indic digits
    ],
)
def test_spellings_of_the_same_word_fold_together(written, same_as):
    assert normalize(written) == normalize(same_as)


def test_padding_is_dropped_but_the_request_is_not():
    groups = query_tokens("عايز تيشيرت اسود لو سمحت")
    # Two meaningful tokens survive: the garment and the colour.
    assert len(groups) == 2


def test_a_query_that_is_only_padding_matches_everything():
    """«لو سمحت» on its own is not "no results", it is "no filter"."""
    assert query_tokens("لو سمحت") == []
    assert matches("anything at all", "لو سمحت") is True


# --- the trap that made this subtle --------------------------------------


def test_a_match_may_not_start_in_the_middle_of_a_word():
    """`tshirt` is a substring of `swea·tshirt·s`.

    A plain `in` check therefore returned every hoodie for "عايز تيشيرت",
    which reads as the bot ignoring what was asked.
    """
    assert matches("Hoodies & Sweatshirts", "تيشيرت") is False
    assert matches("Boxy REHLA Tee T-Shirts", "تيشيرت") is True


def test_prefixes_still_match():
    """`hoodie` has to find `hoodies`."""
    assert matches("Hoodies & Jackets", "هودي") is True
    assert matches("Rehla Yoga Pants wide-leg", "يوجا") is True


# --- against the real catalog --------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("هودي بينك", "Rehla Pink Hoodie"),
        ("هودي ابيض", "Rehla Off White Hoodie"),
        ("جاكيت", "Rehla Jacket"),
        ("بنطلون واسع", "Rehla Yoga Pants"),
        ("يوجا", "Rehla Yoga Pants"),
        ("اوف شولدر", "Rehla Lace Off Shoulder Top"),
        ("محجبات", "Rehla Long Sleeve Off Shoulder Top"),
        ("كاب اسود", "Rehla Black Cap"),
        ("تيشيرت بيبي بلو", "Rehla Baby Blue T-Shirt"),
    ],
)
def test_an_arabic_or_franco_query_finds_the_product(seeded, query, expected):
    found = catalog.get_products(seeded, query=query)
    assert found["count"] > 0, f"{query!r} found nothing"
    assert expected in [product["name"] for product in found["products"]]


def test_a_colour_query_does_not_return_the_whole_shop(seeded):
    """AND across tokens: "تيشيرت اسود" is not "every t-shirt and every black thing"."""
    tees = catalog.get_products(seeded, query="عايز تيشيرت اسود لو سمحت")
    names = [product["name"] for product in tees["products"]]
    assert names, "no t-shirts matched"
    assert "Rehla Pink Hoodie" not in names
    assert "Rehla Jacket" not in names
    assert "Rehla Black Cap" not in names


def test_an_english_query_still_works(seeded):
    """The layer translates *into* the catalog's language; it must not break it."""
    assert catalog.get_products(seeded, query="pink hoodie")["count"] == 1
    assert catalog.get_products(seeded, query="cap")["count"] == 2


def test_a_question_mark_is_not_part_of_the_last_word():
    """`_NON_WORD` keeps the whole Arabic block, and Arabic's punctuation
    lives in it -- so «؟» and «،» stayed glued to the word in front of them.

    Every question a customer types ends in one, which made the last word of
    almost every real query the one word the synonym table could not
    translate: «نص كم؟» is not «نص كم», and «هودي؟» is not «هودي».
    """
    assert normalize("عايز هودي؟") == normalize("عايز هودي")
    assert normalize("اسود، ولا ابيض؟") == normalize("اسود ولا ابيض")
    assert matches("REHLA Hoodie Hoodies & Sweatshirts", "عندكم هودي؟") is True
