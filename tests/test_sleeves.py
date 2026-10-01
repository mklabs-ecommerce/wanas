"""Sleeve length: the words, the field, and the answer behind them.

A customer asked for «البولو النص كم» and was told the shop has two polos and
"no published data about sleeve length for either", with an offer to fetch a
person. Two holes behind that: the phrase was not in the catalog's vocabulary,
and nothing recorded sleeve length at all.

The first fix closed both and left a third open. It recorded the half-sleeve
list and left everything else unset, so the same sentence came back for every
hoodie, jacket and sweatpant in the shop -- which is worse than the original
bug, because it is the shop saying it does not know what it sells, about most
of what it sells.

Every seeded product records its sleeve length, and the dashboard asks for
it. What is *not* recorded stays not recorded: a later version of this module
filled it from the category ("T-Shirts -> long"), and on 2026-09-25 that sold
a short-sleeved tee as long-sleeved in five replies. An unclassified product
reads `sleeve: None`, and the bot says it is not sure
(`tests/test_production_2026_09_25_review.py`).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from assistant.tools.base import ToolContext, call_tool
from config.settings import DATA_DIR
from domain.models import Product
from domain.seed.products import backfill_sleeves
from domain.services import catalog, sleeves
from domain.services.search_terms import matches
from tests.conftest import FIXTURE_CATALOG

# --------------------------------------------------------------------------
# the words
# --------------------------------------------------------------------------

#: Every spelling the shop listed, plus the two a phone keyboard produces by
#: accident: the diacritics «نُص كُم» and the missing space «نصكم». A missing
#: space is not a diacritic -- `normalize` folds one and not the other -- so
#: the joined forms are listed explicitly rather than assumed.
HALF_SLEEVE_WORDS = [
    "نص كم", "نُص كُم", "نصكم", "نصف كم", "كم قصير", "هاف",
    "half sleeve", "short sleeve", "half",
]
LONG_SLEEVE_WORDS = ["كم طويل", "لونج", "لونج سليف", "long sleeve", "long"]
SLEEVELESS_WORDS = ["بدون كم", "سليفلس", "sleeveless"]

HALF_HAY = "Knitted Polo Polo Shirts unisex " + sleeves.SEARCH_TEXT["half"]
LONG_HAY = "REHLA Hoodie Hoodies unisex " + sleeves.SEARCH_TEXT["long"]


@pytest.mark.parametrize("word", HALF_SLEEVE_WORDS)
def test_every_way_a_customer_writes_half_sleeve_finds_a_half_sleeve_product(word):
    assert matches(HALF_HAY, word) is True


@pytest.mark.parametrize("word", HALF_SLEEVE_WORDS)
def test_half_sleeve_words_do_not_find_a_long_sleeve_product(word):
    assert matches(LONG_HAY, word) is False


@pytest.mark.parametrize("word", LONG_SLEEVE_WORDS)
def test_every_way_a_customer_writes_long_sleeve_resolves(word):
    assert matches(LONG_HAY, word) is True
    assert matches(HALF_HAY, word) is False


@pytest.mark.parametrize("word", SLEEVELESS_WORDS)
def test_sleeveless_resolves_and_is_not_half_sleeve(word):
    hay = "REHLA Sweatpant Joggers unisex " + sleeves.SEARCH_TEXT["sleeveless"]
    assert matches(hay, word) is True
    assert matches(HALF_HAY, word) is False


def test_the_sentence_that_started_this_reaches_the_half_sleeve_products(seeded):
    """«التيشيرت النص كم» -- the definite article, the phrase, and a category
    word, in one query. It used to match nothing, which is why the bot said
    it had no data."""
    found = catalog.get_products(seeded, query="التيشيرت النص كم")["products"]
    assert found
    assert {p["sleeve"] for p in found} == {"half"}


# --------------------------------------------------------------------------
# the field: total, and closed
# --------------------------------------------------------------------------

def _real_seed() -> list[dict]:
    with open(DATA_DIR / "products_seed.json", encoding="utf-8") as fh:
        return json.load(fh)


def _fixture_seed() -> list[dict]:
    with open(FIXTURE_CATALOG / "products_seed.json", encoding="utf-8") as fh:
        return json.load(fh)


def test_every_recorded_sleeve_in_rehlas_catalog_is_a_known_value():
    """Rehla's catalogue is scraped, so a product may have no sleeve recorded
    (`None`: the bot says it is not sure). What *is* recorded must be one of
    the three values, never a free-form string."""
    for raw in _real_seed():
        assert raw.get("sleeve") is None or raw["sleeve"] in sleeves.SLEEVES, raw["product_id"]


def test_rehlas_tees_are_half_sleeve_and_nothing_else_is():
    """The closed set: half-sleeve is what a T-Shirts product is, and a top,
    a hoodie or a jacket is never recorded as half -- a closed list is only
    closed if nothing can fall into it."""
    for raw in _real_seed():
        assert (raw["sleeve"] == "half") is (raw["category"] == "T-Shirts"), raw["product_id"]


def test_the_ones_that_are_not_clothes_for_the_arms_say_sleeveless():
    """`sleeveless` is literally true of a cap or a pair of trousers, and it is
    what lets "is this half sleeve?" about trousers be answered "it's trousers"."""
    for raw in _real_seed():
        if raw["category"] in ("Caps", "Pants"):
            assert raw["sleeve"] == "sleeveless", raw["product_id"]


def test_nothing_is_inferred_from_the_category():
    """The category default is gone, not merely changed: there is no function
    left that turns "T-Shirts" into a sleeve length."""
    assert not hasattr(sleeves, "for_category")
    assert sleeves.recorded(None) is None
    assert sleeves.recorded("") is None
    assert sleeves.recorded("short sleeve") == "half"


def test_a_row_sitting_null_reads_as_not_recorded(seeded):
    """A NULL is reported as a NULL -- never as its category's guess -- and
    no sleeve filter returns it, in either direction."""
    hoodie = seeded.get(Product, "rehla-hoodie")
    hoodie.sleeve = None
    seeded.flush()

    assert catalog.get_variants(seeded, "rehla-hoodie")["sleeve"] is None
    found = catalog.get_products(seeded, query="REHLA Hoodie")["products"]
    assert found[0]["sleeve"] is None
    for value in ("half", "long"):
        filtered = catalog.get_products(seeded, sleeve=value)["products"]
        assert "rehla-hoodie" not in {p["product_id"] for p in filtered}, value


# --------------------------------------------------------------------------
# the answer
# --------------------------------------------------------------------------


def test_asking_for_half_sleeve_lists_exactly_the_half_sleeve_products(seeded):
    found = catalog.get_products(seeded, sleeve="half")["products"]
    assert {p["product_id"] for p in found} == {
        raw["product_id"] for raw in _fixture_seed() if raw["sleeve"] == "half"
    }


def test_asking_for_anything_half_sleeve_in_arabic_lists_only_those(seeded):
    found = catalog.get_products(seeded, query="عندكم حاجة نص كم؟")["products"]
    assert found
    assert {p["sleeve"] for p in found} == {"half"}


def test_a_product_that_is_not_half_sleeve_says_so_rather_than_shrugging(seeded):
    assert catalog.get_variants(seeded, "rehla-hoodie")["sleeve"] == "long"
    assert catalog.get_variants(seeded, "rehla-polo")["sleeve"] == "long"
    assert catalog.get_variants(seeded, "rehla-sweatpant")["sleeve"] == "sleeveless"


def test_a_sleeve_filter_that_matches_nothing_means_nothing(seeded):
    """No `sleeve_unrecorded` any more, and its absence is the point. It told
    "we don't sell one" apart from "nobody wrote it down", which was a real
    distinction while products could be unset. They cannot be."""
    result = catalog.get_products(seeded, category="Hoodies & Sweatshirts", sleeve="half")
    assert result["products"] == []
    assert "sleeve_unrecorded" not in result


def test_the_filter_takes_the_wording_the_model_happens_to_choose(seeded):
    by_half = catalog.get_products(seeded, sleeve="half")["count"]
    assert by_half > 0
    for spelling in ("short sleeve", "Half-Sleeve", "نص كم"):
        assert catalog.get_products(seeded, sleeve=spelling)["count"] == by_half


def test_an_unrecognised_sleeve_word_filters_nothing_rather_than_everything(seeded):
    """The safe direction. A word this code does not know must not turn into
    "we have none of those" about a shelf full of them."""
    everything = catalog.get_products(seeded)["count"]
    assert catalog.get_products(seeded, sleeve="batwing")["count"] == everything


def test_the_facet_offers_every_value_the_shop_actually_has(seeded):
    assert catalog.get_categories(seeded)["sleeves"] == ["half", "long", "sleeveless"]


def test_get_variants_carries_the_sleeve_so_a_yes_no_question_is_answerable(seeded):
    ctx = ToolContext(session=seeded, channel="whatsapp", external_id="201000000009")
    assert call_tool(ctx, "get_variants", {"product_id": "knitted-polo"})["sleeve"] == "half"
    assert call_tool(ctx, "get_variants", {"product_id": "rehla-hoodie"})["sleeve"] == "long"


def test_the_search_tool_accepts_the_sleeve_filter(seeded):
    ctx = ToolContext(session=seeded, channel="whatsapp", external_id="201000000009")
    result = call_tool(ctx, "get_products", {"category": "Polo Shirts", "sleeve": "half"})
    assert [p["product_id"] for p in result["products"]] == ["knitted-polo"]


# --------------------------------------------------------------------------
# reaching the data from both directions
# --------------------------------------------------------------------------


def test_the_backfill_fills_every_null_not_only_the_listed_ones(seeded):
    """The bug this file's docstring describes, at its source. Filling only
    the products the seed names is what left the rest answering "I don't
    know"."""
    for product in seeded.scalars(select(Product)).all():
        product.sleeve = None
    seeded.flush()

    result = backfill_sleeves(seeded)

    assert len(result["updated"]) == len(_fixture_seed())
    for product in seeded.scalars(select(Product)).all():
        assert product.sleeve in sleeves.SLEEVES, product.product_id
    assert seeded.get(Product, "knitted-polo").sleeve == "half"
    assert seeded.get(Product, "rehla-hoodie").sleeve == "long"
    assert seeded.get(Product, "rehla-sweatpant").sleeve == "sleeveless"


def test_the_backfill_leaves_a_staff_answer_alone(seeded):
    """Staff set sleeve length from the dashboard, and a boot-time backfill
    that reasserted the seed every deploy would quietly undo them."""
    polo = seeded.get(Product, "knitted-polo")
    polo.sleeve = "long"  # a staff member said so, whatever the seed thinks
    seeded.flush()

    assert backfill_sleeves(seeded)["updated"] == []
    assert polo.sleeve == "long"


def test_a_product_the_seed_never_heard_of_keeps_no_sleeve(seeded):
    """`oversized-plain-t-shirt` reached production through the dashboard,
    not the seed. A product the seed cannot name has no sleeve length the
    backfill knows, and it keeps saying so."""
    seeded.add(
        Product(
            product_id="mystery-parka",
            name="Mystery Parka",
            category="Jackets",
            department="unisex",
            style=[],
            sizes=[],
            colors=[],
            lengths=[],
            price=0,
            original_price=0,
            images=[],
            color_images={},
            source_products=[],
        )
    )
    seeded.flush()

    backfill_sleeves(seeded)
    assert seeded.get(Product, "mystery-parka").sleeve is None


def test_a_new_shopify_product_arrives_with_no_sleeve_recorded(seeded):
    """Shopify has no sleeve field, so `product_import` has nothing to mirror,
    and it mirrors nothing -- not the category's guess."""
    from integrations.shopify.admin_products import _mirror_local

    _mirror_local(
        seeded,
        product_id="brand-new-hoodie",
        title="BRAND NEW HOODIE",
        description="straight from Shopify Admin",
        category="Hoodies & Sweatshirts",
        department="unisex",
        style=[],
        collection=None,
        size_chart=None,
        variants=[{"size": "M", "color": "Black", "price": 700, "stock_qty": 2}],
        image_url=None,
    )
    assert seeded.get(Product, "brand-new-hoodie").sleeve is None


def test_a_shopify_admin_edit_does_not_wipe_what_staff_recorded(seeded):
    """Shopify has nothing to say about sleeve length, so it must say nothing
    rather than saying null."""
    from integrations.shopify.admin_products import _mirror_local

    assert seeded.get(Product, "knitted-polo").sleeve == "half"
    _mirror_local(
        seeded,
        product_id="knitted-polo",
        title="KNITTED POLO",
        description="edited in Shopify Admin",
        category="Polo Shirts",
        department="unisex",
        style=[],
        collection=None,
        size_chart=None,
        variants=[{"size": "S", "color": "Olive", "price": 700, "stock_qty": 3}],
        image_url=None,
    )
    assert seeded.get(Product, "knitted-polo").sleeve == "half"


def test_a_blank_from_the_dashboard_records_not_known(seeded):
    """The edit form offers «مش متسجّل». Saving it records exactly that -- not
    the category's guess."""
    from integrations.shopify.admin_products import _mirror_local

    _mirror_local(
        seeded,
        product_id="knitted-polo",
        title="KNITTED POLO",
        description="",
        category="Polo Shirts",
        department="unisex",
        style=[],
        collection=None,
        size_chart=None,
        variants=[{"size": "S", "color": "Olive", "price": 700, "stock_qty": 3}],
        image_url=None,
        sleeve="",
    )
    assert seeded.get(Product, "knitted-polo").sleeve is None


# --------------------------------------------------------------------------
# the filter the model actually types
# --------------------------------------------------------------------------


def test_a_category_the_model_wrote_from_memory_still_finds_the_shelf(seeded):
    """`category="hoodies"` is not `Hoodies & Sweatshirts`, and the filter was
    an exact match -- so the whole shelf vanished before any other filter ran.
    """
    for spelling in ("hoodies", "Hoodies", "hoodie", "هودي"):
        found = catalog.get_products(seeded, category=spelling)["products"]
        assert {p["category"] for p in found} == {"Hoodies & Sweatshirts"}, spelling


def test_an_exact_category_name_is_not_widened(seeded):
    """"T-Shirts" and "Polo Shirts" share a word. Exact wins, or asking for
    tees would start returning polos."""
    found = catalog.get_products(seeded, category="T-Shirts")["products"]
    assert {p["category"] for p in found} == {"T-Shirts"}


def test_a_word_that_is_no_category_at_all_is_read_as_free_text(seeded):
    """Not as a filter that empties the shop. "we have nothing like that" has
    to come from the catalog, never from the model mistyping a facet."""
    found = catalog.get_products(seeded, category="Ringer")["products"]
    assert [p["product_id"] for p in found] == ["ringer-tee"]


def test_the_half_sleeve_hoodie_question_answers_none_rather_than_unknown(seeded):
    """The live failure, end to end. The model sent `category="hoodies"` with
    `sleeve="half"`; the answer is a truthful "we have none", not a shrug."""
    result = catalog.get_products(seeded, category="hoodies", sleeve="half")
    assert result["products"] == []
    assert catalog.get_products(seeded, category="hoodies")["count"] == 6
