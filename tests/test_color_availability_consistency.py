"""The two colour lists on get_products must tell one story.

A direct «عندك رمادي؟» used to be answered from the absence of positive stock
evidence while a general «إيه الألوان؟» read the descriptive `colors` run --
two sources, two answers, one contradiction turn to turn. `in_stock_colors` is
now the single source both questions answer from; these tests pin it to the
seed's stock rows.
"""

from __future__ import annotations

import pytest

from chatbot.tools.base import ToolContext, call_tool, load_all

load_all()


@pytest.fixture()
def ctx(seeded):
    return ToolContext(session=seeded, channel="whatsapp", external_id="201000000001")


def call(ctx, name, **arguments):
    return call_tool(ctx, name, arguments)


def _by_id(ctx, **filters):
    return {p["product_id"]: p for p in call(ctx, "get_products", **filters)["products"]}


def test_in_stock_colors_is_never_wider_than_colors(ctx):
    """The offer list is a slice of the description, never a new fact."""
    for product in call(ctx, "get_products")["products"]:
        assert set(product["in_stock_colors"]) <= set(product["colors"]), product["product_id"]


def test_sold_out_colourways_stay_described_but_are_not_offered(ctx):
    # Black and Grey are fully sold out in the seed; `colors` still names them,
    # which is correct -- what changed is that they cannot be offered.
    product = _by_id(ctx, query="sweatpant")["wanas-sweatpant"]
    assert {"Black", "Grey", "Olive"} <= set(product["colors"])
    assert product["any_in_stock"] is True
    assert product["in_stock_colors"] == ["Olive"]


def test_a_colour_with_live_stock_is_always_in_the_offer(ctx):
    # Grey sells S/M/XL in the seed (L is gone). The direct grey question and
    # the general colours question now read the same list.
    products = _by_id(ctx, query="sweatpant")
    assert "Grey" in products["lightweight-sweatpant"]["in_stock_colors"]
