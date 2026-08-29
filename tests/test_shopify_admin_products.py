"""`integrations/shopify/admin_products.py`: create/edit a product from
the dashboard, and the local wanas.db mirror that makes it sellable by the
bot afterward.

Exercises the real `create_product` / `update_product` orchestration against
`FakeShopify`'s seam functions -- never a mock of the whole function -- so
the local-DB mirroring in these tests is the same code a real deploy runs.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.models import Product, Variant
from integrations.shopify import admin_products as sap

VARIANT = "wanas-hoodie-s-olive"  # a seeded variant, for product_gid resolution


def test_create_product_writes_shopify_and_local_rows(seeded):
    result = sap.create_product(
        seeded,
        title="Test Beanie",
        description="A warm beanie",
        category="Tops",
        department="unisex",
        style=["Casual"],
        collection=None,
        size_chart=None,
        variants=[
            {"size": "One Size", "price": 250, "stock_qty": 10},
        ],
        image_url="https://example.com/beanie.jpg",
    )
    assert result["product_id"] == "test-beanie"

    product = seeded.get(Product, "test-beanie")
    assert product is not None
    assert product.category == "Tops"
    assert product.department == "unisex"
    assert product.style == ["Casual"]
    assert product.price == Decimal("250")
    assert product.images == ["https://example.com/beanie.jpg"]

    variant = seeded.get(Variant, "test-beanie-one-size")
    assert variant is not None
    assert variant.stock_qty == 10
    assert variant.price == Decimal("250")


def test_create_product_marks_on_sale_when_original_price_is_higher(seeded):
    sap.create_product(
        seeded,
        title="Sale Cap",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[{"size": "One Size", "price": 100, "original_price": 150, "stock_qty": 5}],
    )
    variant = seeded.get(Variant, "sale-cap-one-size")
    assert variant.on_sale is True
    assert variant.original_price == Decimal("150")

    product = seeded.get(Product, "sale-cap")
    assert product.on_sale is True
    assert product.original_price == Decimal("150")


def test_create_product_lands_on_the_fake_shelf(seeded, shopify):
    sap.create_product(
        seeded,
        title="Shelf Cap",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[{"size": "One Size", "price": 300, "stock_qty": 7}],
    )
    assert shopify.qty("shelf-cap-one-size") == 7


def test_create_product_with_no_variants_is_rejected(seeded):
    with pytest.raises(sap.ProductRejected):
        sap.create_product(
            seeded,
            title="Empty",
            description="",
            category="Tops",
            department="unisex",
            style=None,
            collection=None,
            size_chart=None,
            variants=[],
        )


def test_a_second_product_with_the_same_title_gets_a_distinct_id(seeded):
    first = sap.create_product(
        seeded, title="Dup", description="", category="Tops", department="unisex",
        style=None, collection=None, size_chart=None,
        variants=[{"size": "S", "price": 100, "stock_qty": 1}],
    )
    second = sap.create_product(
        seeded, title="Dup", description="", category="Tops", department="unisex",
        style=None, collection=None, size_chart=None,
        variants=[{"size": "S", "price": 100, "stock_qty": 1}],
    )
    assert first["product_id"] != second["product_id"]


def test_product_gid_for_variant_id_resolves_a_seeded_product(seeded, shopify):
    gid = sap.product_gid_for_variant_id(VARIANT)
    assert gid is not None
    assert shopify.products[gid]["title"] == "WANAS Hoodie"


def test_update_product_changes_local_fields(seeded):
    result = sap.update_product(seeded, "wanas-hoodie", category="Hoodies & Sweatshirts", collection="WINTER COLLECTION")
    assert "error" not in result

    product = seeded.get(Product, "wanas-hoodie")
    assert product.collection == "WINTER COLLECTION"


def test_update_product_pushes_title_to_shopify(seeded, shopify):
    sap.update_product(seeded, "wanas-hoodie", title="WANAS Hoodie (renamed)")
    gid = shopify.variant_to_product[VARIANT]
    assert shopify.products[gid]["title"] == "WANAS Hoodie (renamed)"

    product = seeded.get(Product, "wanas-hoodie")
    assert product.name == "WANAS Hoodie (renamed)"


def test_update_product_edits_variant_price_and_stock(seeded, shopify):
    sap.update_product(
        seeded,
        "wanas-hoodie",
        variant_updates=[{"variant_id": VARIANT, "price": 999, "stock_qty": 42}],
    )
    assert shopify.shelf[VARIANT]["price"] == Decimal("999.00")
    assert shopify.qty(VARIANT) == 42

    variant = seeded.get(Variant, VARIANT)
    assert variant.price == Decimal("999")
    assert variant.stock_qty == 42


def test_update_product_on_an_unknown_product_id_is_refused(seeded):
    result = sap.update_product(seeded, "not-a-real-product", title="x")
    assert result["error"] == "product_not_found"


def test_update_product_still_applies_local_fields_with_no_shopify_variants(seeded):
    """A product row with no variants at all (should not happen in practice,
    but nothing here may crash on it) still takes the local-only edit."""
    from domain.models import Product as ProductModel

    orphan = ProductModel(
        product_id="orphan",
        name="Orphan",
        category="Tops",
        department="unisex",
        price=0,
        original_price=0,
    )
    seeded.add(orphan)
    seeded.flush()

    result = sap.update_product(seeded, "orphan", department="women")
    assert "error" not in result
    assert seeded.get(ProductModel, "orphan").department == "women"


# --------------------------------------------------------------------------
# a photo per colourway
# --------------------------------------------------------------------------


def test_a_photo_belongs_to_its_colour_not_to_the_product(seeded, shopify):
    """The reason the form asks for a picture per row rather than one per
    product: `LiveVariant.image_url` is what decides which photo the bot sends
    when a customer names a colour."""
    sap.create_product(
        seeded,
        title="Colour Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[
            {"size": "S", "color": "Olive", "price": 300, "stock_qty": 1},
            {"size": "M", "color": "Olive", "price": 300, "stock_qty": 1},
            {"size": "S", "color": "Navy", "price": 300, "stock_qty": 1},
        ],
        images=[
            {"color": "Olive", "source": "https://x/olive.png"},
            {"color": "Navy", "source": "https://x/navy.png"},
        ],
    )

    assert shopify.variant_images["colour-tee-s-olive"] == "https://x/olive.png"
    assert shopify.variant_images["colour-tee-m-olive"] == "https://x/olive.png"
    assert shopify.variant_images["colour-tee-s-navy"] == "https://x/navy.png"


def test_the_first_photo_for_a_colour_is_that_colours_photo(seeded, shopify):
    """Three sizes in Navy means the colour arrives three times. The extra
    pictures are still attached to the product; they just do not take the
    colour over."""
    sap.create_product(
        seeded,
        title="Repeat Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[
            {"size": "S", "color": "Navy", "price": 300, "stock_qty": 1},
            {"size": "M", "color": "Navy", "price": 300, "stock_qty": 1},
        ],
        images=[
            {"color": "Navy", "source": "https://x/first.png"},
            {"color": "Navy", "source": "https://x/second.png"},
        ],
    )

    assert shopify.variant_images["repeat-tee-s-navy"] == "https://x/first.png"
    assert shopify.variant_images["repeat-tee-m-navy"] == "https://x/first.png"
    product = seeded.get(Product, "repeat-tee")
    assert product.color_images == {"Navy": ["https://x/first.png"]}
    assert len(shopify.media) == 2


def test_a_colour_spelt_differently_is_still_the_same_colour(seeded, shopify):
    sap.create_product(
        seeded,
        title="Case Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[{"size": "S", "color": "Camel Brown", "price": 300, "stock_qty": 1}],
        images=[{"color": "camel  brown", "source": "https://x/camel.png"}],
    )

    assert shopify.variant_images["case-tee-s-camel-brown"] == "https://x/camel.png"


def test_a_colour_with_no_photo_of_its_own_gets_none(seeded, shopify):
    """Showing the Navy photo on the Olive variant is the confident wrong
    answer the whole colour split exists to prevent."""
    sap.create_product(
        seeded,
        title="Partial Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[
            {"size": "S", "color": "Navy", "price": 300, "stock_qty": 1},
            {"size": "S", "color": "Olive", "price": 300, "stock_qty": 1},
        ],
        images=[{"color": "Navy", "source": "https://x/navy.png"}],
    )

    assert shopify.variant_images["partial-tee-s-navy"] == "https://x/navy.png"
    assert "partial-tee-s-olive" not in shopify.variant_images


def test_one_unlabelled_photo_still_covers_every_variant(seeded, shopify):
    """The older single-picture form, and a product nobody split by colour:
    an unlabelled photo is fine, it is a mislabelled one that is not."""
    sap.create_product(
        seeded,
        title="Plain Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[{"size": "S", "price": 300, "stock_qty": 1}, {"size": "M", "price": 300, "stock_qty": 1}],
        image_url="https://x/plain.png",
    )

    assert shopify.variant_images["plain-tee-s"] == "https://x/plain.png"
    assert shopify.variant_images["plain-tee-m"] == "https://x/plain.png"
    assert seeded.get(Product, "plain-tee").images == ["https://x/plain.png"]


def test_a_product_with_no_photos_at_all_asks_shopify_for_nothing(seeded, shopify):
    sap.create_product(
        seeded,
        title="Bare Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[{"size": "S", "price": 300, "stock_qty": 1}],
    )

    assert shopify.media == {}
    assert shopify.variant_images == {}


def test_an_uploaded_chart_is_set_on_shopify_and_kept_locally(seeded, shopify):
    """Two consumers, two copies: the metafield is what the storefront panel
    renders, the local url is what the bot sends without asking Shopify."""
    result = sap.create_product(
        seeded,
        title="Chart Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[{"size": "S", "price": 300, "stock_qty": 1}],
        size_chart_file_gid="gid://shopify/MediaImage/chart",
        size_chart_url="https://cdn/chart.png",
    )

    assert shopify.chart_metafields[result["shopify_id"]] == "gid://shopify/MediaImage/chart"
    assert seeded.get(Product, "chart-tee").size_chart_image == "https://cdn/chart.png"


# --------------------------------------------------------------------------
# the shapes Shopify's own input types insist on
# --------------------------------------------------------------------------


def test_an_option_value_is_an_object_not_a_string():
    """`OptionCreateInput.values` is `[OptionValueCreateInput!]`. A bare list
    of strings fails the whole document with "Expected \"L\" to be a
    key-value object" -- which is every product this ever tried to create."""
    options = sap._build_options(
        [
            {"size": "M", "color": "black"},
            {"size": "L", "color": "navy"},
        ]
    )

    assert options[0] == {"name": "Size", "values": [{"name": "L"}, {"name": "M"}]}
    assert options[1] == {"name": "Color", "values": [{"name": "black"}, {"name": "navy"}]}


def test_the_sku_travels_under_the_inventory_item(seeded, shopify):
    """`ProductVariantsBulkInput` has no `sku` field; it belongs to
    `InventoryItemInput`. The fake asserts the same thing, so this is the
    test that says why."""
    sap.create_product(
        seeded,
        title="Shape Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[{"size": "S", "price": 300, "stock_qty": 1}],
    )

    assert seeded.get(Variant, "shape-tee-s") is not None
    assert shopify.qty("shape-tee-s") == 1

