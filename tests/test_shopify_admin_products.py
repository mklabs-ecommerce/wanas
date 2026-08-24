"""`backend/services/shopify_admin_products.py`: create/edit a product from
the dashboard, and the local wanas.db mirror that makes it sellable by the
bot afterward.

Exercises the real `create_product` / `update_product` orchestration against
`FakeShopify`'s seam functions -- never a mock of the whole function -- so
the local-DB mirroring in these tests is the same code a real deploy runs.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.services import shopify_admin_products as sap
from domain.models import Product, Variant

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
