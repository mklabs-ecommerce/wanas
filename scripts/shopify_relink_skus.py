"""Make the database's variant ids the Shopify store's SKUs. Shopify wins.

The catalogue was first built from a scrape in which four products had no SKU
and one SKU was shared by two variants, so those rows got a generated
`handle-colour-size` id. The store has since been given its own SKUs, and a
variant whose id is not a SKU on Shopify is one the bot serves stale numbers
for (it falls back to the local row). This reports, and with `--apply`
repairs, exactly two things:

1. **A SKU Shopify holds twice** (same SKU on two variants -- Shopify allows
   it, the bot cannot). One write per variant: the SKU becomes the database
   id of the row that variant matches (same product handle, size and colour).
2. **A database variant whose id is no SKU on Shopify**, matched to the one
   Shopify variant with the same product handle, size and colour, has its id
   rewritten to that SKU. Refused for any variant something else already
   references (an order line, a cart line, a waitlist entry) -- this repo's
   production database has none, and a rewrite that leaves them dangling is
   the damage this refuses to do.

Nothing is deleted. Dry-run by default.

    python scripts/shopify_relink_skus.py                 # report
    python scripts/shopify_relink_skus.py --apply         # Shopify fix, then the database
    python scripts/shopify_relink_skus.py --apply --seed-file data/products_seed.json
        # also rewrite the ids in the seed, so a fresh database starts right

The match is by *Shopify's own handle*, never by title, and by size and
colour compared with punctuation and case folded and a typo allowance (the
store spells "Burghandy"; the catalogue says "Burgundy").
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, select, text  # noqa: E402

from domain.db import SessionLocal  # noqa: E402
from domain.models import Product, Variant  # noqa: E402
from integrations.shopify.client import get_admin_client  # noqa: E402

READ = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id handle
      variants(first: 100) {
        nodes { id sku selectedOptions { name value } }
      }
    }
  }
}
"""

UPDATE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku }
    userErrors { field message }
  }
}
"""

CLOSE_ENOUGH = 0.8


def _fold(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _option(node: dict, *names: str) -> str:
    for opt in node.get("selectedOptions") or []:
        if (opt.get("name") or "").lower() in names:
            return opt.get("value") or ""
    return ""


def read_shopify() -> list[dict]:
    client = get_admin_client()
    out, cursor = [], None
    while True:
        page = client(READ, {"cursor": cursor})["products"]
        for product in page["nodes"]:
            for v in product["variants"]["nodes"]:
                out.append(
                    {
                        "gid": v["id"],
                        "product_gid": product["id"],
                        "handle": product["handle"],
                        "sku": (v.get("sku") or "").strip(),
                        "size": _fold(_option(v, "size")),
                        "color": _fold(_option(v, "color", "colour")),
                    }
                )
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def _same_colour(a: str, b: str) -> bool:
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= CLOSE_ENOUGH


def _find(rows: list[dict], handle: str, size: str, color: str) -> list[dict]:
    return [
        r
        for r in rows
        if r["handle"] == handle and r["size"] == size and _same_colour(r["color"], color)
    ]


def references(db) -> list[tuple[str, str]]:
    """(table, column) for every foreign key that points at variants.variant_id."""
    found = []
    inspector = inspect(db.connection())  # the session's own connection: SQLite has one writer
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] == "variants":
                found.append((table, fk["constrained_columns"][0]))
    return found


def plan(db, shop: list[dict]) -> dict:
    by_sku: dict[str, list[dict]] = defaultdict(list)
    for row in shop:
        if row["sku"]:
            by_sku[row["sku"]].append(row)
    shop_skus = set(by_sku)

    local = list(db.scalars(select(Variant)))
    product_ids = {p.product_id for p in db.scalars(select(Product))}

    duplicates, rekey, unmatched, claimed = [], [], [], set()

    # 1. a SKU held twice: each holder gets the id of the database row it is.
    dup_skus = {sku for sku, rows in by_sku.items() if len(rows) > 1}
    for v in local:
        if v.variant_id in shop_skus and v.variant_id not in dup_skus:
            claimed.add(v.variant_id)
    for sku in sorted(dup_skus):
        for holder in by_sku[sku]:
            mates = [
                v
                for v in local
                if v.product_id == holder["handle"]
                and _fold(v.size) == holder["size"]
                and _same_colour(_fold(v.color), holder["color"])
                and v.variant_id not in shop_skus
            ]
            if len(mates) == 1:
                duplicates.append(
                    {
                        "shopify_variant": holder["gid"],
                        "product_gid": holder["product_gid"],
                        "handle": holder["handle"],
                        "from_sku": sku,
                        "to_sku": mates[0].variant_id,
                    }
                )
                claimed.add(mates[0].variant_id)
            else:
                unmatched.append(("duplicate holder", holder["handle"], sku, len(mates)))

    # 2. a database id that is no SKU on Shopify.
    taken = {d["to_sku"] for d in duplicates}
    for v in local:
        if v.variant_id in shop_skus or v.variant_id in taken:
            continue
        candidates = [
            r
            for r in _find(shop, v.product_id, _fold(v.size), _fold(v.color))
            if r["sku"] and r["sku"] not in dup_skus and r["sku"] not in {x["new"] for x in rekey}
        ]
        # Only a Shopify SKU the database does not already use for another row.
        candidates = [r for r in candidates if r["sku"] not in {x.variant_id for x in local}]
        if len(candidates) == 1:
            rekey.append({"old": v.variant_id, "new": candidates[0]["sku"], "handle": v.product_id})
        else:
            unmatched.append(("database variant", v.product_id, v.variant_id, len(candidates)))

    only_shopify = sorted(
        s for s in shop_skus if s not in {v.variant_id for v in local} and s not in {r["new"] for r in rekey}
    )
    return {
        "duplicates": duplicates,
        "rekey": rekey,
        "unmatched": unmatched,
        "shopify_only": only_shopify,
        "products_missing_locally": sorted({r["handle"] for r in shop} - product_ids),
    }


def apply_shopify(duplicates: list[dict]) -> None:
    client = get_admin_client()
    for item in duplicates:
        data = client(
            UPDATE,
            {
                "productId": item["product_gid"],
                "variants": [{"id": item["shopify_variant"], "inventoryItem": {"sku": item["to_sku"]}}],
            },
        )["productVariantsBulkUpdate"]
        if data.get("userErrors"):
            sys.exit(f"Shopify refused {item['handle']}: {data['userErrors']}")
        print(f"  shopify: {item['handle']} {item['from_sku']} -> {item['to_sku']}")


def apply_database(db, rekey: list[dict]) -> None:
    refs = references(db)
    for item in rekey:
        for table, column in refs:
            used = db.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :v"), {"v": item["old"]}
            ).scalar()
            if used:
                sys.exit(f"refusing: {item['old']} is referenced by {used} row(s) in {table}.{column}")
    for item in rekey:
        db.execute(
            text("UPDATE variants SET variant_id = :new WHERE variant_id = :old"),
            {"new": item["new"], "old": item["old"]},
        )
    db.commit()


def rewrite_seed(path: Path, mapping: dict[str, str]) -> int:
    original = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in original else "\n"
    raw = json.loads(original)
    n = 0
    for product in raw:
        for v in product["variants"]:
            if v["variant_id"] in mapping:
                v["variant_id"] = mapping[v["variant_id"]]
                n += 1
    text_out = json.dumps(raw, ensure_ascii=False, indent=1) + "\n"
    path.write_bytes(text_out.replace("\n", newline).encode("utf-8"))
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--seed-file", type=Path, help="also rewrite variant ids in this seed JSON")
    args = ap.parse_args()

    shop = read_shopify()
    with SessionLocal() as db:
        result = plan(db, shop)
        print(f"shopify variants: {len(shop)}   database variants: {db.query(Variant).count()}")
        print(f"Shopify SKUs held twice (will be fixed on Shopify): {len(result['duplicates'])}")
        for d in result["duplicates"]:
            print(f"  {d['handle']}: {d['from_sku']} -> {d['to_sku']}")
        print(f"database ids to rewrite to Shopify's SKU: {len(result['rekey'])}")
        for r in result["rekey"][:6]:
            print(f"  {r['old']} -> {r['new']}")
        if len(result["rekey"]) > 6:
            print(f"  ... {len(result['rekey']) - 6} more")
        print(f"unmatched (left alone): {len(result['unmatched'])}")
        for u in result["unmatched"][:10]:
            print("  ", u)
        print(f"Shopify SKUs with no database row: {len(result['shopify_only'])}")
        print(f"Shopify products with no database row: {result['products_missing_locally']}")
        if not args.apply:
            print("\ndry run: nothing written. Re-run with --apply.")
            return
        if result["unmatched"]:
            sys.exit("refusing to apply with unmatched rows; read them above")
        apply_shopify(result["duplicates"])
        apply_database(db, result["rekey"])
        print(f"database: {len(result['rekey'])} variant id(s) rewritten")
        if args.seed_file:
            mapping = {r["old"]: r["new"] for r in result["rekey"]}
            print(f"seed: {rewrite_seed(args.seed_file, mapping)} id(s) rewritten")


if __name__ == "__main__":
    main()
