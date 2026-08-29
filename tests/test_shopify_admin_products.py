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


def test_the_options_are_declared_when_the_product_is_created(seeded, shopify):
    """Not bolted on afterwards. A product created without them keeps the
    default `Title` option Shopify made it with, and every real variant is
    then refused with "Option does not exist"."""
    result = sap.create_product(
        seeded,
        title="Option Tee",
        description="",
        category="Tops",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[
            {"size": "S", "color": "Olive", "price": 300, "stock_qty": 1},
            {"size": "M", "color": "Navy", "price": 300, "stock_qty": 1},
        ],
    )

    assert shopify.product_options[result["shopify_id"]] == [
        {"name": "Size", "values": [{"name": "M"}, {"name": "S"}]},
        {"name": "Color", "values": [{"name": "Navy"}, {"name": "Olive"}]},
    ]

# --------------------------------------------------------------------------
# changing a photo on a product that already exists
# --------------------------------------------------------------------------


def _olive_variants(session):
    return sorted(
        v.variant_id
        for v in session.get(Product, "wanas-hoodie").variants
        if v.color == "Olive"
    )


def test_a_new_photo_covers_the_whole_colourway_not_just_the_row(seeded, shopify):
    """A photo is of a colour. Leaving M/Olive on the old picture while
    S/Olive has the new one hands `catalog._overlay_images` two photos for one
    colour and the customer whichever came first."""
    olive = _olive_variants(seeded)
    assert len(olive) > 1, "this test needs a colour with more than one size"

    sap.update_product(
        seeded,
        "wanas-hoodie",
        variant_images=[{"variant_id": olive[0], "source": "https://x/new-olive.png"}],
    )

    assert {shopify.variant_images[v] for v in olive} == {"https://x/new-olive.png"}


def test_another_colour_is_left_alone(seeded, shopify):
    olive = _olive_variants(seeded)
    black = [
        v.variant_id for v in seeded.get(Product, "wanas-hoodie").variants if v.color == "Black"
    ]

    sap.update_product(
        seeded,
        "wanas-hoodie",
        variant_images=[{"variant_id": olive[0], "source": "https://x/new-olive.png"}],
    )

    assert not any(v in shopify.variant_images for v in black)


def test_the_local_fallback_keeps_the_colours_nobody_changed(seeded, shopify):
    """`color_images` is what the bot sends when Shopify is unreachable, so a
    write here has to be additive -- replacing the map would blank every
    colour the staff member did not touch."""
    product = seeded.get(Product, "wanas-hoodie")
    product.color_images = {"Black": ["data/images/black.png"]}
    seeded.flush()

    sap.update_product(
        seeded,
        "wanas-hoodie",
        variant_images=[{"variant_id": _olive_variants(seeded)[0], "source": "https://x/new-olive.png"}],
    )

    product = seeded.get(Product, "wanas-hoodie")
    assert product.color_images["Black"] == ["data/images/black.png"]
    assert product.color_images["Olive"] == ["https://x/new-olive.png"]
    assert product.images[0] == "https://x/new-olive.png"


def test_changing_a_photo_touches_neither_price_nor_stock(seeded, shopify):
    variant = seeded.get(Variant, _olive_variants(seeded)[0])
    price_before, qty_before = variant.price, shopify.qty(variant.variant_id)

    sap.update_product(
        seeded,
        "wanas-hoodie",
        variant_images=[{"variant_id": variant.variant_id, "source": "https://x/new-olive.png"}],
    )

    assert seeded.get(Variant, variant.variant_id).price == price_before
    assert shopify.qty(variant.variant_id) == qty_before


def test_a_variant_id_that_is_not_this_products_is_ignored_not_guessed(seeded, shopify):
    sap.update_product(
        seeded,
        "wanas-hoodie",
        variant_images=[{"variant_id": "no-such-variant", "source": "https://x/nope.png"}],
    )

    assert shopify.variant_images == {}
    assert shopify.media == {}

# --------------------------------------------------------------------------
# the three things a created product needs before anyone can see it
# --------------------------------------------------------------------------


def _create(session, **kwargs):
    defaults = {
        "title": "Visible Tee",
        "description": "",
        "category": "T-Shirts",
        "department": "unisex",
        "style": None,
        "collection": None,
        "size_chart": None,
        "variants": [{"size": "S", "price": 300, "stock_qty": 1}],
    }
    return sap.create_product(session, **{**defaults, **kwargs})


def test_a_new_product_is_published_to_the_online_store(seeded, shopify):
    """`status: ACTIVE` only means "not a draft". Until it is published to the
    Online Store it has no storefront url and appears in no collection on the
    site -- which is how the first product created here came out invisible."""
    result = _create(seeded)

    assert result["shopify_id"] in shopify.published
    assert result["warnings"] == []


def test_a_shop_that_cannot_publish_still_gets_its_product(seeded, shopify):
    """A token without the publications scope is a product that exists and
    sells through the bot but is not on the website yet. Refusing the whole
    creation over it would be the worse answer -- saying nothing would be the
    worst."""
    shopify.publish_problem = "add the read_publications and write_publications scopes"

    result = _create(seeded)

    assert seeded.get(Product, result["product_id"]) is not None
    assert result["warnings"] == ["add the read_publications and write_publications scopes"]


def test_the_brand_is_the_shops_not_shopifys_default(seeded, shopify):
    """Left unset, Shopify stamps the *store's* name on it ("My Store"), which
    is not what the products already on the shelf say."""
    from config.settings import settings

    result = _create(seeded, title="Vendor Tee")

    assert shopify.products[result["shopify_id"]]["vendor"] == settings.shopify_vendor


def test_a_manual_collection_is_joined_when_one_is_chosen(seeded, shopify):
    result = _create(seeded, title="Joined Tee", collection="Winter Collection",
                     collection_gid="gid://shopify/Collection/1")

    assert shopify.collection_members["gid://shopify/Collection/1"] == [result["shopify_id"]]
    assert seeded.get(Product, result["product_id"]).collection == "Winter Collection"


def test_a_label_with_no_gid_stays_a_label(seeded, shopify):
    """A smart collection's membership is its rules' business -- on this shop,
    the product's category. Asking Shopify to add a manual member earns a
    refusal, so the picker sends no gid for one."""
    result = _create(seeded, title="Smart Tee", collection="T-Shirts")

    assert shopify.collection_members == {}
    assert seeded.get(Product, result["product_id"]).collection == "T-Shirts"
