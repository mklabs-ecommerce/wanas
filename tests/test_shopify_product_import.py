"""A product added straight in Shopify Admin -- not through the dashboard's
own create panel -- gets no wanas.db row, and `catalog.get_products` (the
bot's search) only ever reads wanas.db. See
`backend/services/shopify_product_import.py` for the full story.
"""

from __future__ import annotations

from decimal import Decimal

from backend.models import Product, Variant
from backend.services.catalog import get_products
from backend.services.shopify_product_import import import_missing_products


def _seed_manual_product(shopify, *, title="Loose Cargo Pants", category="Joggers & Sweatpants", sku=""):
    """A product that exists on the (fake) store but was never pushed through
    `shopify_admin_products.create_product` -- exactly what staff adding a
    product straight in Shopify Admin would leave behind: a real product with
    no wanas.db-recognised SKU on its variant."""
    gid = shopify.shopify_create_product(title=title, description="A pair of pants.", category=category)
    shopify.shopify_create_variants(
        gid,
        [
            {
                "sku": sku,
                "price": "450.00",
                "optionValues": [{"optionName": "Size", "name": "One Size"}],
            }
        ],
    )
    return gid


def test_nothing_already_known_gets_reimported(seeded, shopify):
    """Every product `seed_from` copied onto the fake shelf already has a
    matching wanas.db SKU -- there is nothing here to do."""
    report = import_missing_products(seeded, apply=True)
    assert report["imported"] == []
    assert report["problems"] == []


def test_a_product_added_straight_in_shopify_admin_becomes_searchable(seeded, shopify):
    _seed_manual_product(shopify, title="Loose Cargo Pants")

    report = import_missing_products(seeded, apply=True)
    seeded.commit()

    assert report["problems"] == []
    assert len(report["imported"]) == 1
    product_id = report["imported"][0]["product_id"]

    product = seeded.get(Product, product_id)
    assert product is not None
    assert product.name == "Loose Cargo Pants"
    assert product.department == "unisex"
    assert product.category == "Joggers & Sweatpants"

    variants = seeded.query(Variant).filter_by(product_id=product_id).all()
    assert len(variants) == 1
    assert variants[0].price == Decimal("450.00")

    # And it is now what the bot's own search actually finds.
    found = get_products(seeded, query="cargo")
    assert any(p["product_id"] == product_id for p in found["products"])


def test_the_sku_is_written_back_to_shopify_so_live_price_and_stock_track_it(seeded, shopify):
    gid = _seed_manual_product(shopify, title="Loose Cargo Pants")

    report = import_missing_products(seeded, apply=True)
    seeded.commit()
    product_id = report["imported"][0]["product_id"]

    variant = seeded.query(Variant).filter_by(product_id=product_id).first()
    detail = shopify.get_product(gid)
    assert detail["variants"][0]["sku"] == variant.variant_id


def test_a_dry_run_reports_but_writes_nothing(seeded, shopify):
    _seed_manual_product(shopify, title="Loose Cargo Pants")

    report = import_missing_products(seeded, apply=False)

    assert len(report["imported"]) == 1
    assert seeded.get(Product, report["imported"][0]["product_id"]) is None


def test_a_variant_carrying_someone_elses_sku_is_reported_not_guessed(seeded, shopify):
    """A non-blank SKU that just is not one of ours was set on purpose by
    someone -- report it, do not silently re-key it."""
    _seed_manual_product(shopify, title="Loose Cargo Pants", sku="STAFF-TYPED-THIS")

    report = import_missing_products(seeded, apply=True)

    assert report["imported"] == []
    assert len(report["problems"]) == 1
    assert "Loose Cargo Pants" in report["problems"][0]


def test_running_it_twice_does_not_create_the_product_a_second_time(seeded, shopify):
    _seed_manual_product(shopify, title="Loose Cargo Pants")
    import_missing_products(seeded, apply=True)
    seeded.commit()

    second = import_missing_products(seeded, apply=True)
    seeded.commit()

    assert second["imported"] == []
    assert second["problems"] == []
    assert seeded.query(Product).filter(Product.name == "Loose Cargo Pants").count() == 1


def test_a_draft_product_is_not_imported(seeded, shopify):
    gid = _seed_manual_product(shopify, title="Unfinished Prototype")
    shopify.products[gid]["status"] = "DRAFT"

    report = import_missing_products(seeded, apply=True)

    assert report["imported"] == []
    assert report["problems"] == []
    assert seeded.query(Product).filter(Product.name == "Unfinished Prototype").count() == 0
