"""
Import script: Wanas Gallery product catalog (Excel) -> Product DB seed data (JSON).

Assumptions applied (agreed in design discussion, adjust here if they change):
- DEFAULT_STOCK: every product/variant not marked "Sold Out? = Yes" starts at 10 units.
  Products marked "Sold Out? = Yes" in the source get 0, not the default.
- DEFAULT_LOW_STOCK_THRESHOLD: 2 units, applied uniformly. Adjust per product later
  from the dashboard once real sell-through data exists.
- Stock is tracked at the product level (or product+color level for selectable-color
  items) -- NOT broken down per size, since the source sheet doesn't distinguish
  stock by size either.
- "Selectable variant" products (Color Type column) become ONE product record with a
  `variants` list (one entry per color, each with its own stock_qty) -- not separate
  catalog entries per color. "Single color (fixed)" products stay as single records
  with one `color` field.
- Current selling price = discounted price when On Sale? = Yes, else original price.
  Both are kept on the record so the discount can be shown.

Run:  python3 import_catalog.py
Reads:  wanas_product_catalog.xlsx (same folder)
Writes: products_seed.json (same folder)
"""

import json
import re
from pathlib import Path

import pandas as pd

SOURCE = Path(__file__).parent / "wanas_product_catalog.xlsx"
OUTPUT = Path(__file__).parent / "products_seed.json"

DEFAULT_STOCK = 10
DEFAULT_LOW_STOCK_THRESHOLD = 2

CATEGORY_SHEETS = ["T-SHIRTS", "TOPS", "BOTTOMS", "CAIROKEE MERCH", "WINTER COLLECTION"]


def slugify(text: str) -> str:
    s = str(text).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def parse_list(cell) -> list:
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    return [item.strip() for item in str(cell).split(",") if item.strip()]


def clean_str(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value).strip()


def clean_number(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def build_products() -> list:
    products = []
    seen_ids = set()

    for sheet in CATEGORY_SHEETS:
        df = pd.read_excel(SOURCE, sheet_name=sheet)

        for _, row in df.iterrows():
            name = clean_str(row.get("Product Name"))
            if not name:
                continue

            sizes = parse_list(row.get("Sizes"))
            colors = parse_list(row.get("Available Colors"))
            color_type = (clean_str(row.get("Color Type")) or "").lower()
            is_selectable = color_type.startswith("selectable")

            sold_out = (clean_str(row.get("Sold Out?")) or "").lower() == "yes"
            on_sale = (clean_str(row.get("On Sale?")) or "").lower() == "yes"

            original_price = clean_number(row.get("Original Price (EGP)"))
            discounted_price = clean_number(row.get("Discounted Price (EGP)"))
            price = discounted_price if (on_sale and discounted_price is not None) else original_price

            images = parse_list(row.get("Image URLs (comma-separated)"))
            notes = clean_str(row.get("Notes"))
            source_url = clean_str(row.get("Product URL"))

            base_id = slugify(name)
            pid = base_id
            n = 2
            while pid in seen_ids:
                pid = f"{base_id}-{n}"
                n += 1
            seen_ids.add(pid)

            product = {
                "product_id": pid,
                "name": name,
                "category": sheet,
                "sizes": sizes,
                "price": price,
                "original_price": original_price,
                "on_sale": on_sale,
                "low_stock_threshold": DEFAULT_LOW_STOCK_THRESHOLD,
                "reference_images": images,
                "description": notes,
                "source_url": source_url,
            }

            if is_selectable:
                product["color_selectable"] = True
                product["variants"] = [
                    {"color": c, "stock_qty": 0 if sold_out else DEFAULT_STOCK}
                    for c in colors
                ]
            else:
                product["color_selectable"] = False
                product["color"] = colors[0] if colors else None
                product["stock_qty"] = 0 if sold_out else DEFAULT_STOCK

            products.append(product)

    return products


if __name__ == "__main__":
    products = build_products()
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

    fixed = sum(1 for p in products if not p["color_selectable"])
    selectable = sum(1 for p in products if p["color_selectable"])
    total_units = sum(
        (p.get("stock_qty") or 0) if not p["color_selectable"]
        else sum(v["stock_qty"] for v in p["variants"])
        for p in products
    )
    print(f"Wrote {len(products)} products to {OUTPUT.name}")
    print(f"  - {fixed} single-color products")
    print(f"  - {selectable} selectable-color products")
    print(f"  - {total_units} total stock units assumed across the catalog")
