"""Sleeve length: the words, the field, and the answer behind them.

A customer asked for «البولو النص كم» and was told the shop has two polos and
"no published data about sleeve length for either", with an offer to fetch a
person. Both halves of that were true, and both were fixable: the phrase was
not in the catalog's vocabulary, and nothing in the catalog recorded sleeve
length at all. These tests pin both halves, plus the two directions the data
has to be reachable from -- the dashboard, and a Shopify Admin edit that must
not wipe it.
"""

from __future__ import annotations

import json

import pytest

from assistant.tools.base import ToolContext, call_tool
from config.settings import DATA_DIR
from domain.models import Product
from domain.seed.products import backfill_sleeves
from domain.services import catalog, sleeves
from domain.services.search_terms import matches

# --------------------------------------------------------------------------
# the words
# --------------------------------------------------------------------------

#: Every spelling the brief listed, plus the two a phone keyboard produces by
#: accident: the diacritics «نُص كُم» and the missing space «نصكم». A missing
#: space is not a diacritic -- `normalize` folds one and not the other -- so
#: the joined forms are listed explicitly rather than assumed.
HALF_SLEEVE_WORDS = [
    "نص كم", "نُص كُم", "نصكم", "نصف كم", "كم قصير", "هاف", "هاف سليف",
    "half sleeve", "short sleeve", "nos kom", "half",
]
LONG_SLEEVE_WORDS = ["كم طويل", "لونج", "لونج سليف", "long sleeve", "long"]
SLEEVELESS_WORDS = ["بدون كم", "سليفلس", "sleeveless"]

HALF_HAY = "Knitted Polo Polo Shirts unisex " + sleeves.SEARCH_TEXT["half"]
LONG_HAY = "WANAS Hoodie Hoodies unisex " + sleeves.SEARCH_TEXT["long"]


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
    hay = "Heart Top Tops women " + sleeves.SEARCH_TEXT["sleeveless"]
    assert matches(hay, word) is True
    assert matches(HALF_HAY, word) is False


def test_the_sentence_that_started_this_reaches_the_polo(seeded):
    """«البولو النص كم» -- the definite article, the phrase, and a category
    word, in one query. It used to match nothing, which is why the bot said
    it had no data."""
    found = catalog.get_products(seeded, query="البولو النص كم")["products"]
    assert [p["product_id"] for p in found] == ["knitted-polo"]


def test_asking_for_anything_half_sleeve_lists_only_the_half_sleeve_ones(seeded):
    found = catalog.get_products(seeded, query="عندكم حاجة نص كم؟")["products"]
    assert found
    assert {p["sleeve"] for p in found} == {"half"}


# --------------------------------------------------------------------------
# the field
# --------------------------------------------------------------------------

#: The half-sleeve set, as the shop gave it: fourteen product names off their
#: own list. They are not fourteen products -- the ringer tee, the boxy tee
#: and the knitted polo were each merged out of one Shopify product per
#: colourway, so eleven of those names are colourways of three products.
#:
#: Matched by handle, never by title. Each of these is a `source_products`
#: handle in `products_seed.json`, which is what the shop's own Shopify
#: product was called before the merge -- so the list is checked against the
#: catalog rather than against a guess at what "BOXY WNS GREY TEE" is likely
#: to be.
HALF_SLEEVE_HANDLES = {
    # BROWN / NAVY / BEIGE / BURGANDY RINGER TEE
    "envy-t-shirt-copy", "porche-t-shirt", "beige-ringer-tee", "path-to-heaven-t-shirt",
    # BOXY WNS GREY / OLIVE / BLACK TEE
    "path-to-heaven-t-shirt-copy", "wanas-olive-t-shirt-1", "wanas-grey-t-shirt",
    # OLIVE / WHITE / BURGANDY / NAVY KNITTED POLO
    "olive-knitted-polo", "white-knitted-polo", "burgandy-knitted-polo", "navy-knitted-polo",
    # the three that were never merged
    "cairokee-t-shirt-copy", "cairokee-t-shirt-2", "envy-t-shirt-1",
}


def _seed() -> list[dict]:
    with open(DATA_DIR / "products_seed.json", encoding="utf-8") as fh:
        return json.load(fh)


def test_the_half_sleeve_products_are_exactly_the_ones_the_shop_named():
    """The shop's list is the source of truth, and this is what pins it to it.

    A product is half-sleeve here if and only if every Shopify handle it was
    merged from is on that list. Anything else stays null -- the shop said
    nothing about it, and a hoodie being long-sleeved in every other shop on
    earth is not this shop saying so.
    """
    by_sleeve: dict[str | None, set[str]] = {}
    for raw in _seed():
        handles = {s["handle"] for s in raw.get("source_products") or []}
        expected = "half" if handles <= HALF_SLEEVE_HANDLES else None
        assert raw.get("sleeve") == expected, (
            f"{raw['product_id']} is marked {raw.get('sleeve')!r} but its Shopify "
            f"handles {sorted(handles)} say {expected!r}"
        )
        by_sleeve.setdefault(raw.get("sleeve"), set()).add(raw["product_id"])
    assert by_sleeve["half"] == {
        "ringer-tee", "boxy-wns-tee", "knitted-polo",
        "cairokee-tee", "cairokee-tee-2", "envy-tee",
    }


def test_a_product_the_shop_said_nothing_about_stays_unrecorded(seeded):
    """Not "long", not "sleeveless" -- unrecorded. Inferring a hoodie's sleeve
    length from the fact that it is a hoodie is the invented garment fact this
    field exists to make unnecessary."""
    assert seeded.get(Product, "wanas-hoodie").sleeve is None
    assert seeded.get(Product, "worker-jacket").sleeve is None
    assert seeded.get(Product, "wanas-polo").sleeve is None


def test_unrecorded_products_are_not_swept_up_by_a_sleeve_search(seeded):
    found = catalog.get_products(seeded, sleeve="half")["products"]
    assert "wanas-polo" not in {p["product_id"] for p in found}


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


def test_the_facet_only_offers_values_something_actually_has(seeded):
    assert catalog.get_categories(seeded)["sleeves"] == ["half"]


# --------------------------------------------------------------------------
# the answer
# --------------------------------------------------------------------------


def test_get_variants_carries_the_sleeve_so_a_yes_no_question_is_answerable(seeded):
    ctx = ToolContext(session=seeded, channel="whatsapp", external_id="201000000009")
    assert call_tool(ctx, "get_variants", {"product_id": "knitted-polo"})["sleeve"] == "half"
    assert call_tool(ctx, "get_variants", {"product_id": "wanas-hoodie"})["sleeve"] is None


def test_the_search_tool_accepts_the_sleeve_filter(seeded):
    ctx = ToolContext(session=seeded, channel="whatsapp", external_id="201000000009")
    result = call_tool(ctx, "get_products", {"category": "Polo Shirts", "sleeve": "half"})
    assert [p["product_id"] for p in result["products"]] == ["knitted-polo"]


# --------------------------------------------------------------------------
# reaching the data from both directions
# --------------------------------------------------------------------------


def test_the_backfill_fills_a_null_and_leaves_a_staff_answer_alone(seeded):
    """Production has had these products since before the column existed, so
    the boot backfill is the only thing that reaches them -- and it must never
    be the thing that undoes a staff edit."""
    polo = seeded.get(Product, "knitted-polo")
    polo.sleeve = None
    hoodie = seeded.get(Product, "wanas-hoodie")
    hoodie.sleeve = "long"  # a staff member said so in the dashboard
    seeded.flush()

    result = backfill_sleeves(seeded)

    assert "knitted-polo" in result["updated"]
    assert polo.sleeve == "half"
    # Not on the seed's list at all, and the staff answer stands.
    assert hoodie.sleeve == "long"
    # And running it again changes nothing.
    assert backfill_sleeves(seeded)["updated"] == []


def test_a_shopify_admin_edit_does_not_wipe_what_staff_recorded(seeded):
    """`product_import` mirrors a Shopify product into wanas.db, and Shopify
    has no sleeve field -- so it has nothing to say about sleeve length and
    must say nothing, rather than saying null."""
    from integrations.shopify.admin_products import _mirror_local

    polo = seeded.get(Product, "knitted-polo")
    assert polo.sleeve == "half"

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


def test_staff_can_clear_it_back_to_unrecorded(seeded):
    """Absent means "leave it alone", blank means "I do not know" -- the two
    have to be different, or the dashboard could set a value and never unset
    it."""
    from integrations.shopify.admin_products import _mirror_local

    kwargs = {
        "product_id": "knitted-polo",
        "title": "KNITTED POLO",
        "description": "",
        "category": "Polo Shirts",
        "department": "unisex",
        "style": [],
        "collection": None,
        "size_chart": None,
        "variants": [{"size": "S", "color": "Olive", "price": 700, "stock_qty": 3}],
        "image_url": None,
    }
    _mirror_local(seeded, sleeve="", **kwargs)
    assert seeded.get(Product, "knitted-polo").sleeve is None
    _mirror_local(seeded, sleeve="long sleeve", **kwargs)
    assert seeded.get(Product, "knitted-polo").sleeve == "long"


def test_an_empty_sleeve_filter_says_which_products_were_merely_unrecorded(seeded):
    """"We don't sell one" and "nobody wrote it down" are different sentences,
    and a bare empty list made them the same one.

    Asked for a half-sleeve hoodie, the real model closed that gap itself with
    "all our hoodies are long sleeve" -- a garment fact nothing in the catalog
    states. Every hoodie's sleeve is null; that is the only true answer, and
    this is what makes it available to say.
    """
    result = catalog.get_products(seeded, category="Hoodies & Sweatshirts", sleeve="half")
    assert result["products"] == []
    unrecorded = {p["product_id"] for p in result["sleeve_unrecorded"]}
    assert "wanas-hoodie" in unrecorded
    assert all(seeded.get(Product, pid).sleeve is None for pid in unrecorded)


def test_a_search_with_no_sleeve_filter_does_not_carry_the_key(seeded):
    """It only means something next to a filter. Reporting it everywhere would
    make every ordinary product search carry a list of the shop's unrecorded
    products, which is noise the model would eventually read as a fact."""
    assert "sleeve_unrecorded" not in catalog.get_products(seeded, category="Polo Shirts")


def test_the_recorded_half_sleeve_polo_is_still_found_beside_the_unrecorded_one(seeded):
    result = catalog.get_products(seeded, category="Polo Shirts", sleeve="half")
    assert [p["product_id"] for p in result["products"]] == ["knitted-polo"]
    assert [p["product_id"] for p in result["sleeve_unrecorded"]] == ["wanas-polo"]


# --------------------------------------------------------------------------
# the filter the model actually types
# --------------------------------------------------------------------------


def test_a_category_the_model_wrote_from_memory_still_finds_the_shelf(seeded):
    """`category="hoodies"` is not `Hoodies & Sweatshirts`, and the filter was
    an exact match -- so the whole shelf vanished before any other filter ran.

    Caught live: asked for a half-sleeve hoodie the model sent exactly that,
    got zero rows, and told the customer the shop has no half-sleeve hoodie --
    a claim nothing had checked, because the category had already emptied the
    query.
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


def test_the_live_failure_end_to_end(seeded):
    """The exact call the model made, and the answer it must now be able to
    give: not "we have none", but "nobody recorded it for these"."""
    result = catalog.get_products(seeded, category="hoodies", sleeve="half")
    assert result["products"] == []
    assert {p["product_id"] for p in result["sleeve_unrecorded"]} == {
        "cairokee-hoodie", "wanas-crewneck", "wanas-hoodie",
        "wanas-quarter-zip", "wanas-zip-hoodie", "zipup",
    }
