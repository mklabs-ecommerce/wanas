"""Publishing the size charts to the storefront.

The bot has always had these charts; the storefront never did. What is worth
pinning down is the shape that crosses over, because the consumer is Liquid --
a template language with no way to sort, look up by key, or recover from a
field that is not there. Anything the theme cannot fix has to be right here.
"""

from __future__ import annotations

import json

import pytest

from domain.models import Product
from integrations.shopify import size_charts

CHART = {
    "chart_id": "oversized-hoodie",
    "title": "Oversized hoodie",
    "image": "data/size-charts/oversized-hoodie.png",
    "unit": "cm",
    "measurements": [
        {"key": "width", "label_en": "Width", "label_ar": "العرض", "marker": "A"},
        {"key": "length", "label_en": "Length", "label_ar": "الطول", "marker": "B"},
    ],
    "sizes": {
        "S": {"width": 58, "length": 68},
        "M": {"width": 60, "length": 70},
        "L": {"width": 62, "length": 72},
    },
}


class FakeAdmin:
    """Answers the four calls this module makes, and keeps what it was sent."""

    version = "2026-07"

    def __init__(self, products=(), existing_files=None, existing_definitions=()):
        self.products = list(products)
        self.existing_files = existing_files or {}
        self.existing_definitions = list(existing_definitions)
        self.written = []
        self.created_definitions = []

    def __call__(self, query, variables=None):
        if "metafieldDefinitions(" in query:
            return {
                "metafieldDefinitions": {
                    "nodes": [{"key": k} for k in self.existing_definitions]
                }
            }
        if "metafieldDefinitionCreate" in query:
            key = variables["definition"]["key"]
            if key in self.existing_definitions:
                return {
                    "metafieldDefinitionCreate": {
                        "userErrors": [{"message": "Key is in use", "code": "TAKEN"}]
                    }
                }
            self.created_definitions.append(key)
            self.existing_definitions.append(key)
            return {"metafieldDefinitionCreate": {"createdDefinition": {"id": "gid", "key": key}}}
        if "files(" in query:
            name = variables["query"].split(":", 1)[1]
            gid = self.existing_files.get(name)
            return {"files": {"nodes": [{"id": gid}] if gid else []}}
        if "products(" in query:
            return {
                "products": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": self.products,
                }
            }
        if "metafieldsSet" in query:
            self.written.extend(variables["metafields"])
            return {
                "metafieldsSet": {
                    "metafields": [{"id": "gid", "key": m["key"]} for m in variables["metafields"]],
                    "userErrors": [],
                }
            }
        raise AssertionError(f"unexpected query: {query[:60]}")


def product_node(gid, skus, *, has_image=False):
    return {
        "id": gid,
        "title": gid,
        "metafield": {"id": "gid://metafield/1"} if has_image else None,
        "variants": {"nodes": [{"sku": sku} for sku in skus]},
    }


@pytest.fixture()
def admin(monkeypatch):
    def install(fake):
        monkeypatch.setattr(size_charts, "get_admin_client", lambda: fake)
        return fake

    return install


# --------------------------------------------------------------------------
# the shape Liquid gets
# --------------------------------------------------------------------------


def test_sizes_cross_over_as_a_list_so_the_order_survives():
    """A JSON object's keys come out of Liquid in whatever order they went in,
    and there is no way to sort them back into S / M / L. Ordering it here is
    the only place it can be done."""
    payload = size_charts.storefront_payload(CHART)

    assert [s["name"] for s in payload["sizes"]] == ["S", "M", "L"]
    assert payload["sizes"][0]["values"] == {"width": 58, "length": 68}


def test_both_languages_cross_over_together():
    """The whole reason this is data and not a picture: a chart baked into a
    PNG can only be in one language."""
    payload = size_charts.storefront_payload(CHART)

    assert [(m["label_ar"], m["label_en"]) for m in payload["measurements"]] == [
        ("العرض", "Width"),
        ("الطول", "Length"),
    ]
    assert [m["marker"] for m in payload["measurements"]] == ["A", "B"]


def test_a_measurement_missing_from_one_size_is_still_a_column():
    """Liquid cannot ask whether a key exists. An absent value has to arrive
    as null, or the row silently loses a cell and every column after it shifts
    left under the wrong heading."""
    chart = json.loads(json.dumps(CHART))
    del chart["sizes"]["M"]["length"]

    payload = size_charts.storefront_payload(chart)

    assert payload["sizes"][1]["values"] == {"width": 60, "length": None}


def test_a_chart_with_no_diagram_on_disk_is_not_an_error():
    """`wns-boxy-tee` is exactly this today: real measurements, no picture.
    The table is the part customers read."""
    assert size_charts.chart_image({"image": "data/size-charts/not-here.png"}) is None
    assert size_charts.chart_image({}) is None


# --------------------------------------------------------------------------
# matching products
# --------------------------------------------------------------------------


def test_products_are_matched_to_shopify_by_sku(seeded, admin):
    """`Product.product_id` means nothing to Shopify. The SKU is the only
    thing both sides agree on."""
    charts = {"oversized-hoodie": CHART}
    hoodie = seeded.query(Product).filter(Product.size_chart == "oversized-hoodie").first()
    sku = hoodie.variants[0].variant_id
    fake = admin(FakeAdmin(products=[product_node("gid://shopify/Product/1", [sku])]))

    plan = size_charts.build_plan(seeded, charts, {"oversized-hoodie": "gid://file/1"})

    mine = [e for e in plan["entries"] if e["product_id"] == hoodie.product_id]
    assert len(mine) == 1
    assert mine[0]["gid"] == "gid://shopify/Product/1"
    assert mine[0]["file_gid"] == "gid://file/1"
    assert fake.written == []


def test_a_product_shopify_does_not_have_is_reported_not_guessed(seeded, admin):
    admin(FakeAdmin(products=[]))

    plan = size_charts.build_plan(seeded, {"oversized-hoodie": CHART}, {})

    assert plan["entries"] == []
    assert any(p["chart_id"] == "oversized-hoodie" for p in plan["unmatched"])


def test_a_chart_a_product_names_but_the_file_does_not_have_is_reported(seeded, admin):
    admin(FakeAdmin(products=[]))

    plan = size_charts.build_plan(seeded, {}, {})

    assert "oversized-hoodie" in plan["unknown_charts"]
    assert plan["entries"] == []


def test_a_diagram_set_by_hand_in_admin_is_left_alone(seeded, admin):
    """Somebody uploading a chart in Shopify Admin made a decision; ours is a
    default. The measurements still go up -- only the picture is theirs."""
    hoodie = seeded.query(Product).filter(Product.size_chart == "oversized-hoodie").first()
    sku = hoodie.variants[0].variant_id
    admin(FakeAdmin(products=[product_node("gid://shopify/Product/1", [sku], has_image=True)]))

    plan = size_charts.build_plan(seeded, {"oversized-hoodie": CHART}, {"oversized-hoodie": "gid://file/1"})

    mine = next(e for e in plan["entries"] if e["product_id"] == hoodie.product_id)
    assert mine["kept_existing_image"] is True
    assert mine["file_gid"] is None


def test_replacing_it_is_possible_but_has_to_be_asked_for(seeded, admin):
    hoodie = seeded.query(Product).filter(Product.size_chart == "oversized-hoodie").first()
    sku = hoodie.variants[0].variant_id
    admin(FakeAdmin(products=[product_node("gid://shopify/Product/1", [sku], has_image=True)]))

    plan = size_charts.build_plan(
        seeded,
        {"oversized-hoodie": CHART},
        {"oversized-hoodie": "gid://file/1"},
        replace_images=True,
    )

    mine = next(e for e in plan["entries"] if e["product_id"] == hoodie.product_id)
    assert mine["file_gid"] == "gid://file/1"


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def test_both_metafields_are_written_with_the_types_shopify_expects(admin):
    fake = admin(FakeAdmin())
    entry = {
        "gid": "gid://shopify/Product/1",
        "file_gid": "gid://file/1",
        "data": size_charts.storefront_payload(CHART),
    }

    written = size_charts.write_plan([entry])

    assert written == 2
    by_key = {m["key"]: m for m in fake.written}
    assert by_key[size_charts.DATA_KEY]["type"] == "json"
    assert by_key[size_charts.IMAGE_KEY]["type"] == "file_reference"
    assert by_key[size_charts.IMAGE_KEY]["value"] == "gid://file/1"
    assert json.loads(by_key[size_charts.DATA_KEY]["value"])["sizes"][0]["name"] == "S"


def test_arabic_survives_the_json_encoding(admin):
    """`ensure_ascii` would store \\u0627\\u0644... which renders fine but is
    unreadable to the next person editing it in Admin."""
    fake = admin(FakeAdmin())

    size_charts.write_plan(
        [{"gid": "gid://p/1", "file_gid": None, "data": size_charts.storefront_payload(CHART)}]
    )

    assert "العرض" in fake.written[0]["value"]


def test_a_product_with_no_diagram_still_gets_its_table(admin):
    fake = admin(FakeAdmin())

    size_charts.write_plan(
        [{"gid": "gid://p/1", "file_gid": None, "data": size_charts.storefront_payload(CHART)}]
    )

    assert [m["key"] for m in fake.written] == [size_charts.DATA_KEY]


def test_the_write_is_chunked_to_shopifys_limit(admin):
    """`metafieldsSet` takes 25 at a time and this shop already needs 34."""
    fake = admin(FakeAdmin())
    entries = [
        {"gid": f"gid://p/{i}", "file_gid": f"gid://f/{i}", "data": {"sizes": []}}
        for i in range(20)
    ]

    written = size_charts.write_plan(entries)

    assert written == 40
    assert len(fake.written) == 40


def test_a_definition_that_already_exists_is_not_an_error(admin):
    fake = admin(FakeAdmin(existing_definitions=[size_charts.IMAGE_KEY]))

    created = size_charts.ensure_definitions(apply=True)

    assert created == [size_charts.DATA_KEY]
    assert fake.created_definitions == [size_charts.DATA_KEY]


def test_a_dry_run_writes_nothing_and_says_what_is_left(admin):
    fake = admin(FakeAdmin(existing_definitions=[size_charts.IMAGE_KEY]))

    assert size_charts.ensure_definitions(apply=False) == [size_charts.DATA_KEY]
    assert size_charts.ensure_files({"oversized-hoodie": CHART}, apply=False) == {}
    assert fake.created_definitions == []
    assert fake.written == []


def test_a_diagram_already_in_shopify_files_is_not_uploaded_twice(admin):
    admin(FakeAdmin(existing_files={"oversized-hoodie.png": "gid://file/9"}))

    files = size_charts.ensure_files({"oversized-hoodie": CHART}, apply=True)

    assert files == {"oversized-hoodie": "gid://file/9"}


def test_no_access_to_files_names_the_scope_to_add(admin, monkeypatch):
    from integrations.shopify.client import ShopifyUnavailable

    def denied(query, variables=None):
        raise ShopifyUnavailable('[{"message": "Access denied", "code": "ACCESS_DENIED"}]')

    monkeypatch.setattr(size_charts, "get_admin_client", lambda: denied)

    with pytest.raises(size_charts.SizeChartError, match="write_files"):
        size_charts.ensure_files({"oversized-hoodie": CHART}, apply=True)
