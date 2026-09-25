"""A search that names a product by its full name finds that product.

The live suite, on `main` and on this branch alike:

    customer: «عايز Cairokee T-shirt أسود XL واتنين»
    log:      tool get_products({'query': 'Cairokee T-shirt'})
    bot:      «عندنا اتنين Cairokee T-shirt مش عارف أنهي واحد قصدك»

Every word of "Cairokee T-shirt" is also in "Cairokee T-shirt 2", so the
all-words search found both, and the model -- reasonably, given two hits --
asked a customer who had named the product exactly which one they meant.
"""

from __future__ import annotations

from assistant import action_claims
from domain.services import catalog


def names(session, query: str) -> list[str]:
    return [p["name"] for p in catalog.get_products(session, query=query)["products"]]


def test_the_full_name_finds_that_product_and_not_its_namesake(seeded):
    assert names(seeded, "Cairokee T-shirt") == ["Cairokee T-shirt"]
    assert names(seeded, "Cairokee T-shirt black XL") == ["Cairokee T-shirt"]


def test_the_longer_name_is_the_one_spelt_out(seeded):
    assert names(seeded, "Cairokee T-shirt 2") == ["Cairokee T-shirt 2"]


def test_a_query_that_names_no_product_in_full_still_browses(seeded):
    assert names(seeded, "cairokee tee") == ["Cairokee T-shirt", "Cairokee T-shirt 2"]
    assert len(names(seeded, "hoodie")) > 1


def test_a_single_named_product_is_answered_as_one(seeded):
    """One hit is an answer about that product: its photo rides along and it
    becomes what "المقاسات إيه؟" resolves to."""
    result = catalog.get_products(seeded, query="Cairokee T-shirt")
    assert result["_photo_of"]["product_id"] == "cairokee-tee"


def test_the_word_the_live_run_used_for_added_is_a_claim():
    assert action_claims.unbacked(
        "تمام، اتضفت ليك في السلة", [], cart_has_items=lambda: False, has_orders=lambda: False
    ) == "cart"
