"""One word nobody taught the search must not turn the shelf into "none".

From a real WhatsApp conversation:

    customer: «عايز تيشرت شبه ده ساده أوفر سايز»
    bot:      «مفيش تيشيرت ساده أوفر سايز خالص» + two printed tees
    bot, a minute later: the `oversized plain t-shirt`, 300, size S

«ساده» had no synonym and «سايز» was left over from «أوفر سايز», so the
all-tokens rule vetoed the Arabic query while the English one found the tee.
"""

from __future__ import annotations

from domain.services import catalog
from domain.services.search_terms import matches, query_tokens

PLAIN_TEE = "oversized plain t-shirt T-Shirts unisex oversized black white navy S"


def test_the_customer_s_words_find_the_plain_oversized_tee():
    assert matches(PLAIN_TEE, "عايز تيشرت شبه ده ساده أوفر سايز") is False  # «شبه» stays unknown
    assert matches(PLAIN_TEE, "تيشرت ساده أوفر سايز") is True
    assert matches(PLAIN_TEE, "تيشيرت سادة اوفر سايز") is True
    assert matches(PLAIN_TEE, "تيشرت اوفر سايز") is True
    assert matches(PLAIN_TEE, "تيشرت من غير طباعة") is True


def test_size_is_consumed_only_as_part_of_the_cut_s_name():
    assert query_tokens("اوفر سايز") == [{"اوفر سايز", "oversized"}]
    # On its own it is still a word of the query, not silently dropped.
    assert query_tokens("سايز") == [{"سايز"}]


def test_an_unknown_word_gives_a_partial_match_not_an_empty_shop(seeded):
    result = catalog.get_products(seeded, query="هودي جلد")
    assert result["count"] > 0
    assert result["partial_match"] is True
    assert result["unmatched_terms"] == ["جلد"]
    assert result["products"] == catalog.get_products(seeded, query="هودي")["products"]


def test_a_full_match_carries_no_partial_flag(seeded):
    result = catalog.get_products(seeded, query="هودي")
    assert result["count"] > 0
    assert "partial_match" not in result
    assert "unmatched_terms" not in result


def test_a_query_of_nothing_but_unknown_words_stays_empty_and_says_why(seeded):
    result = catalog.get_products(seeded, query="بيجامه")
    assert result["count"] == 0
    assert result["unmatched_terms"] == ["بيجامه"]
    assert result["partial_match"] is False


def test_a_product_created_in_shopify_is_found_by_its_style(seeded):
    """Production, from the logs: a voice note «السلام عليكم كنت محتاج تيشرت
    اوفر سايز ساده» became `get_products(category='T-shirts',
    style='oversized')`, and the answer was "every oversized tee is printed"
    -- because the plain one was created in Shopify Admin, imported with
    `style=[]`, and the style filter was an exact match on that field."""
    from domain.models import Product

    seeded.add(
        Product(
            product_id="oversized-plain-t-shirt-4",
            name="oversized plain t-shirt",
            category="T-Shirts",
            department="unisex",
            style=[],
            price=300,
            original_price=300,
            description="an oversized fit for maximum comfort",
        )
    )
    seeded.flush()
    names = [
        p["name"]
        for p in catalog.get_products(seeded, category="T-shirts", style="oversized")["products"]
    ]
    assert "oversized plain t-shirt" in names
    # The facet still works for the products that carry it.
    tagged = [
        p["name"]
        for p in catalog.get_products(seeded, style="oversized")["products"]
        if p["style"]
    ]
    assert tagged
