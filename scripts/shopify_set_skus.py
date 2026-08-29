"""Write each wanas.db variant_id into the matching Shopify variant's SKU.

This is the one-time write that gives the chatbot a stable handle on Shopify.
Until it runs, the only way to tie a Shopify variant to a catalog row is to
match on product title plus option values -- which is what shopify_sync.py
does, and which breaks the moment someone fixes a typo in a product name from
the admin. A migration script can survive that because a human reads its diff.
A bot serving customers cannot.

Afterwards, `boxy-wns-tee-xl-black` is the SKU of that variant on Shopify, and
the titles can be edited freely.

    python scripts/shopify_set_skus.py            # dry run, writes nothing
    python scripts/shopify_set_skus.py --apply    # perform the writes

Idempotent: a variant whose SKU is already correct is skipped, so re-running
after adding products only touches the new ones. Stdlib only.

Read the dry run before applying. The title matching used *here* is the same
fragile matching this script exists to retire -- it is acceptable exactly once,
under supervision, and the report is the supervision.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The same explicit map shopify_sync.py uses. Imported rather than copied so
# the two scripts cannot drift apart.
from scripts.shopify_sync import (  # noqa: E402
    PRODUCT_MAP,
    load_env,
    norm,
    shop_variant_key,
    variant_key,
)

PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 25, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title
      variants(first: 100) {
        nodes {
          id sku
          selectedOptions { name value }
        }
      }
    }
  }
}
"""

# bulkUpdate takes up to 250 variants for one product per call, which makes
# this a handful of calls rather than 208.
SKU_MUTATION = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku }
    userErrors { field message }
  }
}
"""


class Shopify:
    """Same minimal client as shopify_sync.py: exits on anything unexpected,
    because a person is watching this run."""

    def __init__(self, domain: str, token: str, version: str):
        self.url = f"https://{domain}/admin/api/{version}/graphql.json"
        self.token = token

    def __call__(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "X-Shopify-Access-Token": self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    payload = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:400]
                if e.code == 401:
                    sys.exit("Shopify rejected the token (401). Check SHOPIFY_ADMIN_TOKEN in .env.")
                if e.code == 403:
                    sys.exit(
                        "Shopify refused the request (403) -- the token is missing a scope.\n"
                        "This script needs write_products.\n" + detail
                    )
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                sys.exit(f"Shopify HTTP {e.code}: {detail}")
            except urllib.error.URLError as e:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                sys.exit(f"Could not reach Shopify: {e.reason}")
        else:
            sys.exit("Shopify kept throttling; try again in a minute.")

        if payload.get("errors"):
            sys.exit("GraphQL error: " + json.dumps(payload["errors"], indent=2)[:900])
        for value in (payload.get("data") or {}).values():
            if isinstance(value, dict) and value.get("userErrors"):
                sys.exit("Shopify rejected a write: " + json.dumps(value["userErrors"], indent=2)[:900])

        cost = (payload.get("extensions") or {}).get("cost", {})
        left = (cost.get("throttleStatus") or {}).get("currentlyAvailable", 1000)
        if left < 200:
            time.sleep(1.5)
        return payload["data"]


def read_db() -> tuple[dict, dict]:
    db = ROOT / "wanas.db"
    if not db.exists():
        sys.exit(f"No database at {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    products = {}
    for row in con.execute("SELECT * FROM products"):
        p = dict(row)
        for field in ("sizes", "colors", "lengths", "style"):
            p[field] = json.loads(p[field]) if p.get(field) else []
        products[p["product_id"]] = p

    variants = {}
    for row in con.execute("SELECT * FROM variants"):
        v = dict(row)
        variants.setdefault(v["product_id"], []).append(v)

    con.close()
    return products, variants


def read_shopify(shop: Shopify) -> list[dict]:
    out, cursor = [], None
    while True:
        data = shop(PRODUCTS_QUERY, {"cursor": cursor})
        block = data["products"]
        out.extend(block["nodes"])
        if not block["pageInfo"]["hasNextPage"]:
            return out
        cursor = block["pageInfo"]["endCursor"]


def build_plan(shop_products, db_products, db_variants):
    """Returns (plan, problems). Talks to nothing."""
    plan: list[dict] = []
    problems: list[str] = []
    already = 0
    seen_db_ids: set[str] = set()
    claimed: dict[str, str] = {}  # sku -> where it was first claimed

    for sp in shop_products:
        title = sp["title"]
        db_id = PRODUCT_MAP.get(title)
        if not db_id:
            problems.append(f"{title!r} is on Shopify but not in PRODUCT_MAP -- skipped entirely")
            continue
        dbp = db_products.get(db_id)
        if not dbp:
            problems.append(f"{title!r} maps to {db_id!r}, which is not in wanas.db")
            continue
        seen_db_ids.add(db_id)

        dbvs = db_variants.get(db_id, [])
        db_by_key = {
            variant_key(v["size"], v["color"], v["length"]): v for v in dbvs
        }

        updates = []
        for sv in sp["variants"]["nodes"]:
            key = shop_variant_key(sv, dbp)
            dbv = db_by_key.get(key)
            if not dbv:
                shown = " / ".join(
                    x["value"] for x in sv["selectedOptions"] if x.get("value")
                )
                problems.append(f"{title}: variant {shown!r} has no row in wanas.db -- no SKU set")
                continue

            want = dbv["variant_id"]
            have = (sv.get("sku") or "").strip()

            if have == want:
                already += 1
                continue

            if want in claimed:
                # Two Shopify variants resolving to one catalog row would give
                # the bot a duplicate SKU and an ambiguous lookup -- worse than
                # no SKU. Report and set neither.
                problems.append(
                    f"{want}: claimed by both {claimed[want]} and {title} -- neither set"
                )
                continue
            claimed[want] = title

            updates.append(
                {
                    "id": sv["id"],
                    "sku": want,
                    "have": have,
                    "label": f"{title} · {norm(dbv['size'])}/{norm(dbv['color'])}",
                }
            )

        if updates:
            plan.append({"product_id": sp["id"], "title": title, "updates": updates})

        for key in set(db_by_key) - {shop_variant_key(sv, dbp) for sv in sp["variants"]["nodes"]}:
            problems.append(f"{title}: {db_by_key[key]['variant_id']} is in wanas.db but not on Shopify")

    for pid in sorted(set(db_products) - seen_db_ids):
        problems.append(f"{db_products[pid]['name']} ({pid}) is in wanas.db but has no Shopify product")

    return plan, problems, already


def report(plan, problems, already):
    def head(t):
        print(f"\n{t}\n" + "-" * len(t))

    if problems:
        head(f"PROBLEMS ({len(problems)}) -- read these first")
        for p in problems:
            print("  ! " + p)

    total = sum(len(p["updates"]) for p in plan)
    overwrites = [u for p in plan for u in p["updates"] if u["have"]]

    if overwrites:
        # An existing SKU may be something the client relies on elsewhere, so
        # replacing one is called out separately from filling a blank.
        head(f"EXISTING SKUs THAT WOULD BE REPLACED ({len(overwrites)})")
        for u in overwrites:
            print(f"  {u['label']}: {u['have']!r} -> {u['sku']!r}")

    if plan:
        head(f"SKUs TO SET ({total})")
        for p in plan:
            print(f"  {p['title']}")
            for u in p["updates"]:
                mark = "~" if u["have"] else "+"
                print(f"    {mark} {u['sku']}")

    head("SUMMARY")
    print(f"  already correct : {already}")
    print(f"  to set          : {total}")
    print(f"  problems        : {len(problems)}")


def apply(shop: Shopify, plan):
    done = 0
    for p in plan:
        variants = [{"id": u["id"], "inventoryItem": {"sku": u["sku"]}} for u in p["updates"]]
        shop(SKU_MUTATION, {"productId": p["product_id"], "variants": variants})
        done += len(variants)
        print(f"  set {len(variants):>3} SKUs on {p['title']}")
    print(f"\nDone: {done} SKUs written.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform the writes")
    args = ap.parse_args()

    env = load_env(ROOT / ".env")
    for key in ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN"):
        if not env.get(key):
            sys.exit(f"{key} is missing from .env")

    shop = Shopify(
        env["SHOPIFY_STORE_DOMAIN"],
        env["SHOPIFY_ADMIN_TOKEN"],
        env.get("SHOPIFY_API_VERSION", "2026-07"),
    )

    db_products, db_variants = read_db()
    shop_products = read_shopify(shop)
    plan, problems, already = build_plan(shop_products, db_products, db_variants)
    report(plan, problems, already)

    if not args.apply:
        print("\nDry run -- nothing was written. Re-run with --apply once the diff looks right.")
        return

    if problems:
        # Applying over unresolved problems would leave the bot with a partial
        # mapping and no obvious sign of it.
        print("\nRefusing to apply while problems are listed above. Fix them, or")
        print("confirm each is expected, then re-run.")
        sys.exit(1)

    if not plan:
        print("\nNothing to do -- every SKU is already correct.")
        return

    print()
    apply(shop, plan)


if __name__ == "__main__":
    main()
