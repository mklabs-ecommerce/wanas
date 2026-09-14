"""Sizes come out in the order a person would say them.

`S`, `M`, `L`, `XL` sort alphabetically to L, M, S, XL, and nothing in this
codebase said otherwise -- so the variant list handed to the model was in that
order and the model recited it back. From the audited conversation:

    Black مقاس S، و Grey مقاس M و S و XL، و Navy مقاس M و L و S

Every size there is really in stock. It still reads as a shop that does not
know its own stock room.
"""

from __future__ import annotations

import pytest

from common.sizes import in_order, sort_key
from domain.services import catalog


def test_the_shops_four_sizes_come_out_smallest_first():
    assert in_order(["XL", "S", "L", "M"]) == ["S", "M", "L", "XL"]


def test_alphabetical_is_what_this_replaces():
    """The bug, stated: plain `sorted` is wrong and looks like it works."""
    assert sorted(["XL", "S", "L", "M"]) == ["L", "M", "S", "XL"]
    assert in_order(["XL", "S", "L", "M"]) != sorted(["XL", "S", "L", "M"])


@pytest.mark.parametrize(
    "written", ["xl", "XL", " XL ", "Xl"]
)
def test_spelling_and_spacing_do_not_change_a_size_s_place(written):
    assert sort_key(written) == sort_key("XL")


def test_one_size_leads_rather_than_trails():
    assert in_order(["M", "One Size", "S"])[0] == "One Size"


def test_numeric_sizes_sort_as_numbers_not_as_text():
    assert in_order(["32", "8", "10"]) == ["8", "10", "32"]


def test_an_unknown_label_is_kept_and_put_last():
    """Never dropped and never scattered among the real ones -- a size this
    module has not heard of is still a size the shop sells."""
    assert in_order(["Tall", "S", "M"]) == ["S", "M", "Tall"]


def test_duplicates_collapse_without_reordering():
    assert in_order(["M", "m", "S"]) == ["S", "M"]


def test_the_variant_list_the_model_reads_is_in_size_order(seeded):
    """The actual path. The model writes the sizes back in the order it was
    given them, so this list *is* the sentence the customer reads."""
    payload = catalog.get_variants(seeded, "boxy-wns-tee")
    for color in ("Black", "Grey", "Olive"):
        sizes = [v["size"] for v in payload["variants"] if v["color"] == color]
        assert sizes == ["S", "M", "L", "XL"], color


def test_the_search_result_lists_sizes_in_order_too(seeded):
    found = catalog.get_products(seeded, query="Boxy WNS Tee")["products"]
    assert found[0]["sizes"] == ["S", "M", "L", "XL"]


def test_a_product_created_from_the_dashboard_is_stored_in_size_order():
    """`_summarize` sorted alphabetically, so the order was wrong in the
    database from the moment the product existed -- not only on the way out."""
    from integrations.shopify.admin_products import _summarize

    summary = _summarize(
        [
            {"size": "XL", "color": "Black", "price": 100},
            {"size": "S", "color": "Black", "price": 100},
            {"size": "L", "color": "Black", "price": 100},
            {"size": "M", "color": "Black", "price": 100},
        ]
    )
    assert summary["sizes"] == ["S", "M", "L", "XL"]
