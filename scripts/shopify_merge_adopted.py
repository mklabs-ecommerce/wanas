"""Fold a Shopify product the bot adopted into the curated row it duplicates.

When Shopify is connected, boot mirrors any store product the database has no
row for. For a product the database *already* held under a generated id (the
scrape's `handle-colour-size` ids, before the store had SKUs) that produced a
second row: same garment, the store's SKUs, but `Uncategorized` / `unisex` and
none of the curated fields (style, sleeve, department) -- while the original
kept the curated fields and SKUs the store does not have.

This pairs them by Shopify's own handle (the old row's `product_id` is the
handle; the adopted row owns that handle's SKUs), copies the curated fields
onto the adopted row **only where it has nothing better than a default**, and
archives the old row (`Product.archived`: off the bot's search, still readable
-- nothing is deleted). Dry-run by default.

    python scripts/shopify_merge_adopted.py
    python scripts/shopify_merge_adopted.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from domain.db import SessionLocal  # noqa: E402
from domain.models import Product, Variant  # noqa: E402
from integrations.shopify.client import get_admin_client  # noqa: E402

READ = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { handle variants(first: 100) { nodes { sku } } }
  }
}
"""

#: field -> the values that mean "nothing was chosen here".
DEFAULTS = {
    "category": (None, "", "Uncategorized"),
    "department": (None, "", "unisex"),
    "style": (None, "", [], ()),
    "collection": (None, ""),
    "sleeve": (None, ""),
    "size_chart": (None, ""),
}


def shopify_handles() -> dict[str, set[str]]:
    client = get_admin_client()
    out: dict[str, set[str]] = {}
    cursor = None
    while True:
        page = client(READ, {"cursor": cursor})["products"]
        for p in page["nodes"]:
            out[p["handle"]] = {
                (v.get("sku") or "").strip() for v in p["variants"]["nodes"] if v.get("sku")
            }
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def pairs(db) -> list[tuple[Product, Product]]:
    found = []
    handles = shopify_handles()
    for handle, skus in handles.items():
        old = db.get(Product, handle)
        if old is None or old.archived:
            continue
        owners = {
            v.product_id
            for v in db.scalars(select(Variant).where(Variant.variant_id.in_(skus)))
        } - {handle}
        if len(owners) == 1:
            found.append((old, db.get(Product, owners.pop())))
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with SessionLocal() as db:
        found = pairs(db)
        print(f"curated rows duplicated by an adopted Shopify product: {len(found)}")
        for old, new in found:
            copy = {
                f: getattr(old, f)
                for f, empty in DEFAULTS.items()
                if getattr(old, f) not in empty
                # The bot's category taxonomy is the curated one (`Pants`, not
                # the store's product type `Trousers`); the rest only fill a default.
                and (getattr(new, f) in empty or (f == "category" and getattr(new, f) != getattr(old, f)))
            }
            print(
                f"  {old.product_id} ({old.name}) -> {new.product_id}: "
                f"copy {sorted(copy)}; archive the old row"
            )
            if args.apply:
                for field, value in copy.items():
                    setattr(new, field, value)
                old.archived = True
        if not args.apply:
            print("\ndry run: nothing written. Re-run with --apply.")
            return
        db.commit()
        print("done: nothing deleted")


if __name__ == "__main__":
    main()
