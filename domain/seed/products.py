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

from config.settings import DATA_DIR
from domain.models import Product, Variant
from domain.services import sleeves

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
        product.sleeve = sleeves.normalise(raw.get("sleeve"))
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


def backfill_sleeves(session: Session, path: Path | None = None) -> dict:
    """Give a seeded product the sleeve length the seed records for it, where
    it has none.

    `import_products` above only ever runs against an empty catalog
    (`app._ensure_catalog_seeded`), so adding a field to the seed file reaches
    a fresh database and nothing else. Production has had these products since
    long before `Product.sleeve` existed, and a column added by
    `domain/schema_drift.py` arrives full of NULLs.

    **Only what the seed says.** A product the seed does not name -- one
    created in the dashboard or in Shopify Admin -- keeps its NULL. This used
    to fill it from the category ("T-Shirts -> long"), and on 2026-09-25 that
    described a short-sleeved tee as long-sleeved to customers in five
    replies. Nobody recording it is not the same as it being long.

    **Only where it is NULL.** Staff set sleeve length from the dashboard, and
    a boot-time backfill that reasserted the seed's answer every deploy would
    quietly undo them.
    """
    from_seed = {
        raw["product_id"]: sleeves.normalise(raw.get("sleeve"))
        for raw in load_seed(path)
    }

    updated: list[str] = []
    for product in session.scalars(select(Product)).all():
        if product.sleeve is not None or not from_seed.get(product.product_id):
            continue
        product.sleeve = from_seed[product.product_id]
        updated.append(product.product_id)
    session.flush()
    return {"updated": updated}


#: Sleeve lengths that were *inferred* rather than recorded, and what the
#: product actually is: `{product_id: (inferred value, true value)}`.
#:
#: `oversized-plain-t-shirt-4` was created in the dashboard on 2026-09-22 with
#: the new-product form's preselected «كم طويل» (or, equally, filled in from
#: its category by the backfill above) -- nothing a person chose. It is a
#: short-sleeved tee: Shopify's own photos of all three colourways show it,
#: and its size chart gives a 21-25 cm sleeve. Rewritten only while it still
#: holds the inferred value, so a choice staff make later is never undone.
CORRECTED_SLEEVES: dict[str, tuple[str, str]] = {
    "oversized-plain-t-shirt-4": ("long", "half"),
}


def correct_sleeves(session: Session) -> dict:
    """Apply `CORRECTED_SLEEVES`, exactly: only a row still holding the
    inferred value is rewritten."""
    updated: list[str] = []
    for product_id, (inferred, true) in CORRECTED_SLEEVES.items():
        product = session.get(Product, product_id)
        if product is not None and product.sleeve == inferred:
            product.sleeve = true
            updated.append(product_id)
    session.flush()
    return {"updated": updated}


#: Size-chart values the seed file itself used to hold and has since
#: corrected, `{product_id: (retired chart_id, ...)}`.
#:
#: `import_products` runs against an empty catalog only, so correcting the
#: seed file corrects every *future* database and none that already exists.
#: That is how the Boxy WNS Tee kept the Ringer tee's chart: it was seeded
#: with `ringer-boxy-tee`, commit e0333cb gave it its own `wns-boxy-tee`, and
#: production -- seeded before that commit -- went on sending customers who
#: asked for the Boxy WNS Tee's measurements the Ringer's. Sizing wrong is a
#: return, and AGENTS.md is explicit that a product must never be answered
#: with another product's chart.
#:
#: A value listed here is one nobody chose: it is the seed's own mistake. Add
#: a line whenever a seed correction changes a product's `size_chart`.
RETIRED_SIZE_CHARTS: dict[str, tuple[str, ...]] = {
    "boxy-wns-tee": ("ringer-boxy-tee",),
}


def correct_retired_size_charts(session: Session, path: Path | None = None) -> dict:
    """Move a product off a chart its seed retired, onto the seed's current one.

    Exact, the same way `backfill_sleeves` is careful: a product is rewritten
    only while it still carries a value listed in `RETIRED_SIZE_CHARTS` for
    that product. Any other value -- a chart staff picked in the dashboard, a
    chart made for it there, a product the seed does not know -- is left
    alone, so running this on every boot can never undo a person's choice.
    Idempotent: once corrected, the retired value is gone and nothing matches.
    """
    current = {raw["product_id"]: raw.get("size_chart") for raw in load_seed(path)}
    updated: list[str] = []
    for product_id, retired in RETIRED_SIZE_CHARTS.items():
        product = session.get(Product, product_id)
        wanted = current.get(product_id)
        if product is None or not wanted or product.size_chart == wanted:
            continue
        if product.size_chart in retired:
            product.size_chart = wanted
            updated.append(product_id)
    session.flush()
    return {"updated": updated}
