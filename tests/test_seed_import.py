"""The seed import -- the same assertions merge_catalog.py makes.

18 products, 208 variants, 114 in stock after loading (AGENTS.md, What to test).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from domain.models import Product, ShippingRate, Variant
from domain.seed.governorates import import_governorates
from domain.seed.products import import_products
from domain.services.size_charts import all_charts, get_chart

EXPECTED_CATEGORY_COUNTS = {
    "T-Shirts": (5, 48),
    "Hoodies & Sweatshirts": (6, 68),
    "Polo Shirts": (2, 28),
    "Joggers & Sweatpants": (2, 24),
    "Jackets": (1, 16),
    "Tops": (2, 24),
}


def test_counts(seeded):
    assert seeded.scalar(select(func.count()).select_from(Product)) == 18
    assert seeded.scalar(select(func.count()).select_from(Variant)) == 208
    assert seeded.scalar(select(func.count()).select_from(Variant).where(Variant.stock_qty > 0)) == 114


def test_stock_seeded_at_ten_or_zero(seeded):
    """Every in-stock variant starts at 10, every out-of-stock one at 0."""
    counts = {row[0] for row in seeded.execute(select(Variant.stock_qty).distinct())}
    assert counts == {0, 10}
    thresholds = {row[0] for row in seeded.execute(select(Variant.low_stock_threshold).distinct())}
    assert thresholds == {2}


def test_taxonomy(seeded):
    rows = seeded.execute(
        select(Product.category, func.count(func.distinct(Product.product_id)), func.count(Variant.variant_id))
        .join(Variant, Variant.product_id == Product.product_id)
        .group_by(Product.category)
    ).all()
    actual = {category: (p, v) for category, p, v in rows}
    assert actual == EXPECTED_CATEGORY_COUNTS

    departments = seeded.execute(select(Product.department, func.count()).group_by(Product.department)).all()
    assert dict(departments) == {"unisex": 16, "women": 2}


def test_collections_are_optional(seeded):
    """8 products belong to no collection, and that is correct, not missing."""
    with_collection = seeded.execute(
        select(Product.collection, func.count()).where(Product.collection.is_not(None)).group_by(Product.collection)
    ).all()
    assert dict(with_collection) == {"WINTER COLLECTION": 7, "CAIROKEE MERCH": 3}
    assert seeded.scalar(select(func.count()).select_from(Product).where(Product.collection.is_(None))) == 8


def test_colour_is_a_variant_axis(seeded):
    """One WANAS Hoodie in three colours, not three hoodies -- and it is
    priced per colour, which is why a product-level price is not quotable."""
    hoodie = seeded.get(Product, "wanas-hoodie")
    assert hoodie is not None
    assert sorted(hoodie.colors) == ["Black", "Grey", "Olive"]
    by_colour = {v.color: float(v.price) for v in hoodie.variants}
    assert by_colour["Grey"] == 700
    assert by_colour["Black"] == 650
    assert by_colour["Olive"] == 650


def test_worker_jacket_has_a_length_axis(seeded):
    jacket = seeded.get(Product, "worker-jacket")
    assert sorted(jacket.lengths) == ["Long", "Short"]
    assert {v.length for v in jacket.variants} == {"Long", "Short"}
    # Every other product's variants carry no length.
    others = seeded.scalars(select(Variant).where(Variant.product_id != "worker-jacket")).all()
    assert all(v.length is None for v in others)


def test_every_variant_has_a_colour(seeded):
    assert seeded.scalar(select(func.count()).select_from(Variant).where(Variant.color.is_(None))) == 0


def test_cairokee_hoodie_is_the_only_fully_sold_out_product(seeded):
    products = seeded.scalars(select(Product)).all()
    sold_out = [p.product_id for p in products if all(v.stock_qty == 0 for v in p.variants)]
    assert sold_out == ["cairokee-hoodie"]


def test_size_charts_cover_every_product(seeded):
    charts = all_charts()
    assert len(charts) == 12
    for product in seeded.scalars(select(Product)).all():
        assert product.size_chart is not None, f"{product.product_id} has no chart mapping"
        assert get_chart(product.size_chart) is not None


def test_conditional_charts():
    """worker-jacket is length-aware; wns-tops has no XL. Both are the cases a
    model would otherwise pattern-match its way past."""
    jacket = get_chart("worker-jacket")
    assert jacket["length_specific"] is True
    applies = {m["key"]: m.get("applies_to_length") for m in jacket["measurements"]}
    assert applies["short_sleeve"] == "Short"
    assert applies["long_sleeve"] == "Long"

    tops = get_chart("wns-tops")
    assert "length_specific" not in tops  # absent, and the tool fills in False
    assert sorted(tops["sizes"]) == ["L", "M", "S"]


def test_image_paths_come_from_the_seed_not_from_the_handle(seeded):
    """The source handle is a meaningless Shopify leftover: the black tee
    lives under a folder called `wanas-grey-t-shirt`."""
    tee = seeded.get(Product, "boxy-wns-tee")
    assert any("wanas-grey-t-shirt" in path for path in tee.images)
    assert set(tee.color_images) <= set(tee.colors)


def test_governorates_seeded_with_blank_fees(seeded):
    assert seeded.scalar(select(func.count()).select_from(ShippingRate)) == 27
    assert seeded.scalar(select(func.count()).select_from(ShippingRate).where(ShippingRate.fee.is_(None))) == 27
    cairo = seeded.get(ShippingRate, "Cairo")
    assert cairo.label_ar == "القاهرة"


def test_reimport_is_idempotent_and_preserves_stock(seeded):
    variant = seeded.get(Variant, "wanas-hoodie-s-olive")
    variant.stock_qty = 3
    seeded.commit()

    stats = import_products(seeded)
    import_governorates(seeded)
    seeded.commit()

    assert stats["products"] == 18
    assert stats["variants"] == 208
    # A re-run must not undo a sale or a staff stock edit.
    assert seeded.get(Variant, "wanas-hoodie-s-olive").stock_qty == 3
    assert seeded.scalar(select(func.count()).select_from(Variant)) == 208


def test_reimport_preserves_a_fee_already_set(seeded):
    rate = seeded.get(ShippingRate, "Cairo")
    rate.fee = 60
    seeded.commit()
    import_governorates(seeded)
    seeded.commit()
    assert float(seeded.get(ShippingRate, "Cairo").fee) == 60


def test_variant_status_is_computed(seeded):
    variant = seeded.get(Variant, "wanas-hoodie-s-olive")
    variant.stock_qty = 0
    assert variant.status == "sold_out"
    variant.stock_qty = 2  # threshold is 2
    assert variant.status == "low_stock"
    variant.stock_qty = 3
    assert variant.status == "in_stock"
    # Sold out fires at zero regardless of the threshold value.
    variant.low_stock_threshold = 0
    variant.stock_qty = 0
    assert variant.status == "sold_out"


@pytest.mark.parametrize("username,password", [("", "longenough"), ("staff", "short")])
def test_create_staff_rejects_bad_input(db, username, password):
    from domain.services.auth import create_staff

    with pytest.raises(ValueError):
        create_staff(db, username, password)


def test_staff_password_round_trip(db):
    from domain.services.auth import authenticate, create_staff

    create_staff(db, "amira", "correct-horse")
    db.commit()
    assert authenticate(db, "amira", "correct-horse") is not None
    assert authenticate(db, "amira", "wrong") is None
    assert authenticate(db, "nobody", "correct-horse") is None


def test_order_and_queue_ids(db):
    from domain.services.ids import next_order_id, next_queue_id

    assert next_order_id(db) == "WNS-1001"
    assert next_order_id(db) == "WNS-1002"
    assert next_queue_id(db, "item_swap") == "SWAP-1"
    assert next_queue_id(db, "handoff") == "HO-2"
    assert next_queue_id(db, "alert") == "ALERT-3"
