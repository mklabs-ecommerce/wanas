"""Catalog import from `data/products_seed.json`.

The seed is already generated. `data/merge_catalog.py` produces it and is not
an incremental sync -- this importer only loads what is already there, and
re-derives nothing: not stock, not pricing, not the taxonomy, and not the
image paths, which are read from the `images` / `color_images` fields because
the source handle is a meaningless Shopify leftover.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Product, Variant
from config.settings import DATA_DIR

SEED_PATH = DATA_DIR / "products_seed.json"

#: The assertions merge_catalog.py makes about its own output, restated here
#: so a bad or truncated seed file fails the import instead of quietly
#: producing a half catalog.
EXPECTED_PRODUCTS = 18
EXPECTED_VARIANTS = 208
EXPECTED_IN_STOCK = 114


class SeedError(RuntimeError):
    pass


def load_seed(path: Path | None = None) -> list[dict]:
    with open(path or SEED_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def import_products(session: Session, path: Path | None = None, *, verify: bool = True) -> dict:
    """Load the catalog. Idempotent: existing rows are updated in place.

    Stock is *not* overwritten on an existing variant. Re-running the import
    against a live database must not undo sales or a staff stock edit; a fresh
    variant gets the seed's count, an existing one keeps its own.
    """
    products = load_seed(path)

    existing_stock = {
        row.variant_id: (row.stock_qty, row.low_stock_threshold)
        for row in session.scalars(select(Variant)).all()
    }

    seen_products: set[str] = set()
    seen_variants: set[str] = set()

    for raw in products:
        product = session.get(Product, raw["product_id"])
        if product is None:
            product = Product(product_id=raw["product_id"])
            session.add(product)

        product.name = raw["name"]
        product.category = raw["category"]
        product.department = raw["department"]
        product.style = raw.get("style") or []
        product.collection = raw.get("collection")
        product.size_chart = raw.get("size_chart")
        product.sizes = raw.get("sizes") or []
        product.colors = raw.get("colors") or []
        product.lengths = raw.get("lengths") or []
        product.price = raw["price"]
        product.original_price = raw["original_price"]
        product.on_sale = bool(raw["on_sale"])
        product.images = raw.get("images") or []
        product.color_images = raw.get("color_images") or {}
        product.description = raw.get("description") or ""
        product.source_products = raw.get("source_products") or []
        seen_products.add(product.product_id)

        for rawv in raw["variants"]:
            variant = session.get(Variant, rawv["variant_id"])
            if variant is None:
                variant = Variant(variant_id=rawv["variant_id"])
                session.add(variant)
            variant.product_id = product.product_id
            variant.size = rawv["size"]
            variant.color = rawv.get("color")
            variant.length = rawv.get("length")
            variant.price = rawv["price"]
            variant.original_price = rawv["original_price"]
            variant.on_sale = bool(rawv["on_sale"])
            if rawv["variant_id"] in existing_stock:
                variant.stock_qty, variant.low_stock_threshold = existing_stock[rawv["variant_id"]]
            else:
                variant.stock_qty = int(rawv["stock_qty"])
                variant.low_stock_threshold = int(rawv.get("low_stock_threshold", 2))
            seen_variants.add(variant.variant_id)

    session.flush()

    stats = {
        "products": len(seen_products),
        "variants": len(seen_variants),
        "in_stock": sum(
            1
            for v in session.scalars(select(Variant)).all()
            if v.variant_id in seen_variants and v.stock_qty > 0
        ),
    }

    if verify and not existing_stock:
        # Only meaningful on a first import: once orders have been placed the
        # in-stock count is supposed to have moved.
        if stats["products"] != EXPECTED_PRODUCTS:
            raise SeedError(f"expected {EXPECTED_PRODUCTS} products, imported {stats['products']}")
        if stats["variants"] != EXPECTED_VARIANTS:
            raise SeedError(f"expected {EXPECTED_VARIANTS} variants, imported {stats['variants']}")
        if stats["in_stock"] != EXPECTED_IN_STOCK:
            raise SeedError(f"expected {EXPECTED_IN_STOCK} in stock, imported {stats['in_stock']}")

    return stats
