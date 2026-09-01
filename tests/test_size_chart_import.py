"""Reading the size-chart metafields back out of Shopify.

`test_size_charts.py` covers the publish. This covers the return trip: a
chart edited in Shopify Admin reaching the bot, without a round trip of our
own publish being mistaken for an edit.
"""

from __future__ import annotations

import json

import pytest

from domain.models import Product, SizeChart
from domain.services import size_charts as local_charts
from integrations.shopify import size_chart_import
from integrations.shopify.size_charts import storefront_payload

CHART = {
    "chart_id": "oversized-hoodie",
    "title": "Oversized Hoodie",
    "unit": "cm",
    "measurements": [
        {"key": "width", "label_en": "Chest", "label_ar": "الصدر"},
        {"key": "length", "label_en": "Length", "label_ar": "الطول"},
    ],
    "sizes": {"S": {"width": 58, "length": 68}, "M": {"width": 60, "length": 70}},
}


def node(gid, skus, *, data=None, image=None, file_gid="gid://shopify/MediaImage/1"):
    """One product as `iter_shopify_charts` hands it on."""
    return {
        "gid": gid,
        "title": gid,
        "skus": list(skus),
        "data": json.dumps(data, ensure_ascii=False) if data is not None else None,
        "image_url": image,
        "image_file_gid": file_gid if image else None,
    }


def skus_of(session, product_id):
    product = session.get(Product, product_id)
    return [v.variant_id for v in product.variants]


# --------------------------------------------------------------------------
# The shape the theme was given, read back
# --------------------------------------------------------------------------


def test_the_publish_round_trips_through_the_theme_shape():
    """Sizes went out as an ordered array because Liquid cannot sort object
    keys. They have to come back as the object the bot reads, in that order."""
    back = size_chart_import.chart_from_payload(storefront_payload(CHART))

    assert back["chart_id"] == "oversized-hoodie"
    assert back["unit"] == "cm"
    assert list(back["sizes"]) == ["S", "M"]
    assert back["sizes"]["S"] == {"width": 58, "length": 68}
    assert [m["key"] for m in back["measurements"]] == ["width", "length"]


def test_a_blank_cell_stays_blank_rather_than_becoming_a_zero():
    """Same rule a vision reading follows: a measurement nobody filled in is
    missing, and a confident 0 is a wrong-size order."""
    payload = storefront_payload(CHART)
    payload["sizes"][0]["values"]["length"] = None

    back = size_chart_import.chart_from_payload(payload)
    assert back["sizes"]["S"] == {"width": 58}


def test_a_column_the_chart_does_not_declare_is_dropped():
    payload = storefront_payload(CHART)
    payload["sizes"][0]["values"]["sleeve"] = 61

    back = size_chart_import.chart_from_payload(payload)
    assert "sleeve" not in back["sizes"]["S"]


@pytest.mark.parametrize(
    "payload",
    [
        {"measurements": [], "sizes": [{"name": "S", "values": {"width": 58}}]},
        {"measurements": [{"key": "width"}], "sizes": []},
        {"measurements": [{"key": "width"}], "sizes": [{"name": "S", "values": {}}]},
        "not an object",
    ],
)
def test_a_chart_with_nothing_quotable_reads_as_no_chart(payload):
    """None, not an empty chart: there is no number here for the bot to say,
    and a chart row with no sizes would answer `has_chart` true and then have
    nothing to show."""
    assert size_chart_import.chart_from_payload(payload) is None


# --------------------------------------------------------------------------
# A round trip is not news
# --------------------------------------------------------------------------


def test_a_chart_shopify_already_agrees_with_is_skipped(seeded):
    """The hoodie's chart is in `data/size_charts.json` and was published from
    it. Reading our own publish back and writing a row would replace a file
    that ships with the code with a CDN url that can 404."""
    file_chart = local_charts.get_chart("oversized-hoodie")
    assert file_chart is not None, "fixture check"

    nodes = [node("gid://p/1", skus_of(seeded, "wanas-hoodie"), data=storefront_payload(file_chart))]
    plan = size_chart_import.build_plan(seeded, nodes)

    assert plan["products"] == []
    assert plan["unchanged"] == ["wanas-hoodie"]


def test_an_edited_measurement_does_come_back(seeded):
    """The case this exists for: somebody fixed a number in Shopify Admin."""
    file_chart = local_charts.get_chart("oversized-hoodie")
    payload = storefront_payload(file_chart)
    payload["sizes"][0]["values"][payload["measurements"][0]["key"]] = 999

    nodes = [node("gid://p/1", skus_of(seeded, "wanas-hoodie"), data=payload)]
    plan = size_chart_import.build_plan(seeded, nodes)
    size_chart_import.apply_plan(seeded, plan)

    chart = local_charts.get_chart("oversized-hoodie", seeded)
    first_size = payload["sizes"][0]["name"]
    assert chart["sizes"][first_size][payload["measurements"][0]["key"]] == 999
    assert seeded.get(SizeChart, "oversized-hoodie").source == "shopify"


def test_the_bot_then_quotes_the_shopify_numbers(seeded):
    """End of the path: the row overlays the file, so `get_size_chart` answers
    with what the product page shows."""
    from assistant.tools.base import ToolContext, call_tool, load_all

    load_all()
    payload = storefront_payload(local_charts.get_chart("oversized-hoodie"))
    key = payload["measurements"][0]["key"]
    payload["sizes"][0]["values"][key] = 999
    plan = size_chart_import.build_plan(
        seeded, [node("gid://p/1", skus_of(seeded, "wanas-hoodie"), data=payload)]
    )
    size_chart_import.apply_plan(seeded, plan)

    ctx = ToolContext(session=seeded, channel="whatsapp", external_id="201000000001")
    result = call_tool(ctx, "get_size_chart", {"product_id": "wanas-hoodie"})
    assert result["sizes"][payload["sizes"][0]["name"]][key] == 999


# --------------------------------------------------------------------------
# A chart authored in Admin, with no id of ours
# --------------------------------------------------------------------------


def test_a_chart_with_no_id_does_not_claim_a_shared_one(seeded):
    """Three t-shirts share `oversized-graphic-tee`. A chart somebody made on
    one of them in Admin must not rewrite the other two's."""
    payload = storefront_payload({**CHART, "chart_id": None})
    nodes = [node("gid://p/2", skus_of(seeded, "cairokee-tee"), data=payload)]

    plan = size_chart_import.build_plan(seeded, nodes)
    assert plan["products"][0]["chart_id"] == "shopify-cairokee-tee"

    size_chart_import.apply_plan(seeded, plan)
    assert seeded.get(Product, "cairokee-tee").size_chart == "shopify-cairokee-tee"
    assert seeded.get(Product, "envy-tee").size_chart == "oversized-graphic-tee"
    assert local_charts.get_chart("oversized-graphic-tee", seeded)["sizes"] != CHART["sizes"]


# --------------------------------------------------------------------------
# The diagram
# --------------------------------------------------------------------------


def test_a_products_own_picture_lands_where_the_bot_looks_for_it(seeded):
    """A chart that is only a picture -- no measurements published -- is what
    `Product.size_chart_image` is for, and the bot sends it as the whole
    answer."""
    product = seeded.get(Product, "wanas-hoodie")
    product.size_chart = None
    seeded.flush()

    url = "https://cdn.shopify.com/chart.png"
    plan = size_chart_import.build_plan(
        seeded, [node("gid://p/1", skus_of(seeded, "wanas-hoodie"), image=url)]
    )
    size_chart_import.apply_plan(seeded, plan)

    assert seeded.get(Product, "wanas-hoodie").size_chart_image == url


def test_a_picture_alongside_a_chart_goes_on_the_chart(seeded):
    payload = storefront_payload({**CHART, "chart_id": None})
    url = "https://cdn.shopify.com/chart.png"
    plan = size_chart_import.build_plan(
        seeded, [node("gid://p/1", skus_of(seeded, "wanas-hoodie"), data=payload, image=url)]
    )
    size_chart_import.apply_plan(seeded, plan)

    row = seeded.get(SizeChart, "shopify-wanas-hoodie")
    assert row.image_url == url
    assert row.image_file_gid == "gid://shopify/MediaImage/1"
    assert seeded.get(Product, "wanas-hoodie").size_chart_image is None


def test_a_picture_does_not_displace_a_chart_the_product_already_has(seeded):
    """The product has a measured chart and Shopify published only a diagram.
    Setting `size_chart_image` would put a picture in front of numbers the bot
    can quote."""
    plan = size_chart_import.build_plan(
        seeded,
        [node("gid://p/1", skus_of(seeded, "wanas-hoodie"), image="https://cdn/x.png")],
    )
    assert plan["products"] == []


def test_a_generic_file_reference_is_read_too():
    """Shopify stores a picture as a MediaImage and anything else -- a PDF, an
    SVG -- as a GenericFile, and staff can attach either."""
    url, gid = size_chart_import._image_of(
        {"reference": {"id": "gid://shopify/GenericFile/9", "url": "https://cdn/chart.svg"}}
    )
    assert (url, gid) == ("https://cdn/chart.svg", "gid://shopify/GenericFile/9")


# --------------------------------------------------------------------------
# Nothing is ever deleted, and nothing acts on an empty read
# --------------------------------------------------------------------------


def test_a_product_with_no_metafields_is_left_alone(seeded):
    """Absence in Shopify is not a statement that the local chart is wrong --
    the twelve shipped charts were never published from Admin at all."""
    before = seeded.get(Product, "wanas-hoodie").size_chart
    plan = size_chart_import.build_plan(
        seeded, [node("gid://p/1", skus_of(seeded, "wanas-hoodie"))]
    )
    size_chart_import.apply_plan(seeded, plan)

    assert plan["products"] == []
    assert seeded.get(Product, "wanas-hoodie").size_chart == before


def test_a_shopify_product_matching_no_local_sku_is_reported_not_guessed(seeded):
    plan = size_chart_import.build_plan(
        seeded, [node("gid://p/9", ["not-a-sku"], data=storefront_payload(CHART))]
    )
    assert plan["products"] == []
    assert plan["unmatched"] == [{"gid": "gid://p/9", "title": "gid://p/9"}]


def test_unparseable_json_is_logged_and_skipped(seeded, caplog):
    """A metafield somebody hand-edited into invalid JSON is not a reason to
    stop, and it is certainly not a reason to write half a chart."""
    bad = node("gid://p/1", skus_of(seeded, "wanas-hoodie"))
    bad["data"] = "{not json"
    with caplog.at_level("WARNING"):
        plan = size_chart_import.build_plan(seeded, [bad])

    assert plan["products"] == []
    assert "not valid JSON" in caplog.text


def test_an_empty_live_read_is_refused(monkeypatch):
    """Same guard `product_reconcile` carries: no products is a token, a scope
    or an outage far more often than it is a shop."""
    monkeypatch.setattr(
        size_chart_import,
        "get_admin_client",
        lambda: lambda q, v: {"products": {"pageInfo": {"hasNextPage": False}, "nodes": []}},
    )
    with pytest.raises(size_chart_import.EmptyRead):
        size_chart_import.iter_shopify_charts()


def test_the_read_follows_every_page(monkeypatch):
    pages = [
        {
            "products": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [
                    {
                        "id": "gid://p/1",
                        "title": "one",
                        "chartImage": None,
                        "chartData": None,
                        "variants": {"nodes": [{"sku": "a"}]},
                    }
                ],
            }
        },
        {
            "products": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "id": "gid://p/2",
                        "title": "two",
                        "chartImage": None,
                        "chartData": None,
                        "variants": {"nodes": [{"sku": "b"}]},
                    }
                ],
            }
        },
    ]
    monkeypatch.setattr(
        size_chart_import, "get_admin_client", lambda: lambda q, v: pages.pop(0)
    )
    assert [n["gid"] for n in size_chart_import.iter_shopify_charts()] == ["gid://p/1", "gid://p/2"]


def test_applying_twice_changes_nothing_the_second_time(seeded):
    """Idempotent, like every other reconcile in here."""
    payload = storefront_payload({**CHART, "chart_id": None})
    nodes = [node("gid://p/1", skus_of(seeded, "wanas-hoodie"), data=payload)]

    size_chart_import.apply_plan(seeded, size_chart_import.build_plan(seeded, nodes))
    second = size_chart_import.build_plan(seeded, nodes)

    assert second["products"] == []
    assert second["unchanged"] == ["wanas-hoodie"]
