"""`get_variants` says what can be bought in each colourway, worked out.

"Which sizes are there in olive, and how much is it?" used to be answered by
the model reading every variant row, matching `in_stock` ids back to sizes,
grouping by colour and ordering S before M -- each step a place for a
sold-out size to be offered or the wrong colour's price to be quoted. The
payload now carries the answer (`by_color`), built from the same live
overlay as every other number in it.
"""

from __future__ import annotations

from assistant.tools.base import ToolContext, call_tool

CHANNEL = "whatsapp"
WHO = "201000000999"


def variants(session, product_id: str) -> dict:
    ctx = ToolContext(session=session, channel=CHANNEL, external_id=WHO)
    return call_tool(ctx, "get_variants", {"product_id": product_id})


def test_each_colourway_says_which_sizes_can_be_bought(seeded):
    by_color = variants(seeded, "wanas-hoodie")["by_color"]

    assert set(by_color) == {"Black", "Grey", "Olive"}
    assert by_color["Grey"]["available"] == [], "grey is sold out in every size"
    assert by_color["Grey"]["sold_out"]
    for colour in ("Black", "Olive"):
        sizes = by_color[colour]["available"]
        assert sizes, colour
        order = ["XS", "S", "M", "L", "XL", "XXL"]
        assert sizes == sorted(sizes, key=order.index), "sizes in the order they are said"


def test_a_colourway_says_what_it_costs_and_what_it_cost(seeded):
    black = variants(seeded, "wanas-hoodie")["by_color"]["Black"]
    assert black["price"] == 650
    assert black["original_price"] == 900


def test_each_colourway_carries_its_own_price(seeded):
    """The WANAS Hoodie is 650 in black and olive and 700 in grey -- a single
    number for the product is 50 pounds wrong for one of them."""
    by_color = variants(seeded, "wanas-hoodie")["by_color"]
    assert by_color["Black"]["price"] == by_color["Olive"]["price"] == 650
    assert by_color["Grey"]["price"] == 700


def test_a_colourway_whose_sizes_differ_in_price_says_so(seeded, shopify):
    shopify.set("ringer-tee-xl-navy", price=620)
    navy = variants(seeded, "ringer-tee")["by_color"]["Navy"]
    assert "price" not in navy
    assert (navy["price_from"], navy["price_to"]) == (580, 620)


def test_the_worker_jackets_length_is_its_own_axis(seeded):
    by_color = variants(seeded, "worker-jacket")["by_color"]
    assert all(" / " in key for key in by_color)
    assert {key.split(" / ")[1] for key in by_color} == {"Long", "Short"}


def test_it_follows_the_live_shelf_not_wanas_db(seeded, shopify):
    shopify.set("wanas-hoodie-s-black", qty=0)
    black = variants(seeded, "wanas-hoodie")["by_color"]["Black"]
    assert "S" not in black["available"]
    assert "S" in black["sold_out"]
