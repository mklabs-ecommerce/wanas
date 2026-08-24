"""Reconcile the Shopify catalog against wanas.db.

The catalog currently on Shopify is a rough import: every variant carries a
30 EGP placeholder price with the real per-colour pricing dumped into the
product description as text, inventory is a flat 10 per variant regardless of
what is actually on the shelf, three products sit under the wrong type, the
Worker Jacket lost its Long/Short dimension, and Zipup never made it across.

This script treats `wanas.db` as the truth and rewrites Shopify to match.

It prints a full diff and changes nothing unless you pass --apply. Read the
diff first: it is the only cheap moment to catch a bad product match.

    python scripts/shopify_sync.py              # dry run, writes nothing
    python scripts/shopify_sync.py --apply      # perform the writes

Stdlib only, so it runs against the project's existing requirements.
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

# Shopify's titles were typed by hand at import time and do not match the
# catalog's names ("WANAS QUARTRZIP" vs "WANAS Quarter-Zip", "RINGER BOXY FIT
# TSHIRT" vs "Ringer Tee"). Fuzzy matching on a live store that takes money is
# not worth the cleverness, so the mapping is explicit and auditable.
PRODUCT_MAP = {
    "WANAS QUARTRZIP": "wanas-quarter-zip",
    "WANAS CREWNECK": "wanas-crewneck",
    "WANAS SWEATPANT": "wanas-sweatpant",
    "WANAS HOODIE": "wanas-hoodie",
    "WANAS ZIP-HOODIE": "wanas-zip-hoodie",
    "WANAS POLO": "wanas-polo",
    "Cairokee Hoodie": "cairokee-hoodie",
    "LIGHTWEIGHT SWEATPANT": "lightweight-sweatpant",
    "FEELIN FINE TOP": "feelin-fine-top",
    "HEART TOP": "heart-top",
    "Envy T-shirt": "envy-tee",
    "WORKER JACKET": "worker-jacket",
    "Cairokee T-shirt 2": "cairokee-tee-2",
    "Cairokee T-shirt": "cairokee-tee",
    "KNITTED POLO": "knitted-polo",
    "BOXY WNS TEE": "boxy-wns-tee",
    "RINGER BOXY FIT TSHIRT": "ringer-tee",
    # Created by shopify_fix_structure.py rather than imported by hand, which
    # is why its title already matches the catalog name -- and why it was
    # missing from a map written before it existed.
    "Zipup": "zipup",
}

# The six categories the storefront's CATEGORY_ORDER renders, in order.
VALID_CATEGORIES = [
    "T-Shirts",
    "Hoodies & Sweatshirts",
    "Polo Shirts",
    "Joggers & Sweatpants",
    "Jackets",
    "Tops",
]


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_env(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"No .env at {path}. Copy .env.example and fill it in.")
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Shopify:
    """Minimal GraphQL client. Retries on Shopify's throttle and on 5xx."""

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
                    sys.exit(
                        "Shopify rejected the token (401).\n"
                        "Check SHOPIFY_ADMIN_TOKEN in .env, and that the app is installed."
                    )
                if e.code == 403:
                    sys.exit(
                        "Shopify refused the request (403).\n"
                        "The token is likely missing a scope. Needed: read/write_products, "
                        "read/write_inventory, read_locations.\n" + detail
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

        # userErrors live per-mutation; surface them rather than silently
        # reporting success for a write that did nothing.
        for value in (payload.get("data") or {}).values():
            if isinstance(value, dict) and value.get("userErrors"):
                sys.exit(
                    "Shopify rejected a write: "
                    + json.dumps(value["userErrors"], indent=2)[:900]
                )

        # Stay well clear of the leaky-bucket limit on long runs.
        cost = (payload.get("extensions") or {}).get("cost", {})
        left = (cost.get("throttleStatus") or {}).get("currentlyAvailable", 1000)
        if left < 200:
            time.sleep(1.5)
        return payload["data"]


# --------------------------------------------------------------------------
# read both sides
# --------------------------------------------------------------------------


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


PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 25, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title productType descriptionHtml
      options { name values }
      variants(first: 100) {
        nodes {
          id title price compareAtPrice
          selectedOptions { name value }
          inventoryItem { id tracked }
          inventoryQuantity
        }
      }
    }
  }
}
"""


def read_shopify(gql: Shopify) -> list[dict]:
    out, cursor = [], None
    while True:
        data = gql(PRODUCTS_QUERY, {"cursor": cursor})["products"]
        out.extend(data["nodes"])
        if not data["pageInfo"]["hasNextPage"]:
            return out
        cursor = data["pageInfo"]["endCursor"]


SCOPE_HELP = (
    "The token is missing the read_locations scope, so stock cannot be written.\n\n"
    "Scopes live on the app version in the Dev Dashboard (dev.shopify.com)\n"
    "> your app > Versions > Create version > API access > Scopes. The list\n"
    "needs read_locations, read_inventory and write_inventory alongside the\n"
    "product scopes. Release the version, then make sure the store's install\n"
    "has picked up the new grant -- if it hasn't, reinstall and copy the\n"
    "reissued token into .env."
)


def probe_location(gql: Shopify) -> tuple[str | None, str | None]:
    """Returns (location_id, error). Never exits.

    A dry run that skips this check is worth very little: it would report a
    clean diff for stock changes the token has no permission to make, and the
    failure would only appear on the run that matters.
    """
    try:
        nodes = gql("{locations(first:5){nodes{id name}}}")["locations"]["nodes"]
    except SystemExit:
        return None, SCOPE_HELP
    if not nodes:
        return None, "The store has no inventory location."
    return nodes[0]["id"], None


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------


def norm(value: str | None) -> str:
    """Option values were typed independently on each side, so 'Camel/Brown'
    and 'Camel Brown' are the same colour spelled two ways. Compare on a
    normalised form; keep the original for display."""
    if not value:
        return ""
    return " ".join(value.replace("/", " ").replace("-", " ").split()).casefold()


def opt(variant: dict, name: str) -> str | None:
    for o in variant["selectedOptions"]:
        if o["name"].lower() == name.lower():
            return o["value"]
    return None


def variant_key(size, color, length) -> tuple:
    return (norm(size), norm(color), norm(length))


def shop_variant_key(sv: dict, dbp: dict) -> tuple:
    """A Shopify variant may simply not carry an option the catalog knows
    about -- the Envy T-shirt has no Color option at all, the Worker Jacket
    has no Length. Where the catalog offers exactly one value for the missing
    option there is no ambiguity, so fill it in rather than reporting a
    mismatch. Where it offers several, leave it blank and let it be reported."""

    def fill(present, candidates):
        if present:
            return present
        return candidates[0] if len(candidates) == 1 else None

    return variant_key(
        opt(sv, "Size"),
        fill(opt(sv, "Color"), dbp.get("colors") or []),
        fill(opt(sv, "Length"), dbp.get("lengths") or []),
    )


def build_plan(shop_products, db_products, db_variants):
    """Returns (plan, problems). Nothing here talks to the network."""
    plan = {"variants": [], "product_type": [], "description": [], "missing": [], "note": []}
    problems = []

    seen_db_ids = set()

    for sp in shop_products:
        title = sp["title"]
        pid = PRODUCT_MAP.get(title)
        if not pid:
            problems.append(f"Shopify product {title!r} is not in PRODUCT_MAP — skipped")
            continue
        if pid not in db_products:
            problems.append(f"{title!r} maps to {pid!r}, which is not in wanas.db — skipped")
            continue
        seen_db_ids.add(pid)

        dbp = db_products[pid]
        dbvs = db_variants.get(pid, [])

        # product type
        want_type = dbp["category"]
        if want_type not in VALID_CATEGORIES:
            problems.append(f"{pid}: category {want_type!r} is not one of the six the storefront renders")
        if (sp.get("productType") or "") != want_type:
            plan["product_type"].append(
                {"id": sp["id"], "title": title, "from": sp.get("productType") or "(none)", "to": want_type}
            )

        # description: strip the injected pricing preamble
        desc = sp.get("descriptionHtml") or ""
        if "Price varies by color" in desc or "->" in desc:
            plan["description"].append(
                {"id": sp["id"], "title": title, "to": dbp.get("description") or ""}
            )

        # variants
        db_by_key = {
            variant_key(v["size"], v["color"], v["length"]): v for v in dbvs
        }
        # keep readable labels for the report, since keys are normalised
        db_label = {
            variant_key(v["size"], v["color"], v["length"]): " / ".join(
                x for x in (v["size"], v["color"], v["length"]) if x
            )
            for v in dbvs
        }
        shop_keys = set()

        for sv in sp["variants"]["nodes"]:
            key = shop_variant_key(sv, dbp)
            shop_keys.add(key)
            dbv = db_by_key.get(key)
            if not dbv:
                shown = " / ".join(
                    x for x in (opt(sv, "Size"), opt(sv, "Color"), opt(sv, "Length")) if x
                )
                problems.append(
                    f"{title}: variant {shown!r} exists on Shopify but not in wanas.db"
                )
                continue

            want_price = f"{dbv['price']:.2f}"
            want_compare = f"{dbv['original_price']:.2f}" if dbv["on_sale"] else None
            have_price = sv["price"]
            have_compare = sv["compareAtPrice"]
            have_qty = sv["inventoryQuantity"] or 0
            want_qty = dbv["stock_qty"]

            price_diff = float(have_price) != float(want_price)
            compare_diff = (have_compare or None) != (want_compare or None)
            qty_diff = have_qty != want_qty

            if price_diff or compare_diff or qty_diff:
                plan["variants"].append(
                    {
                        "product_id": sp["id"],
                        "variant_id": sv["id"],
                        "inventory_item_id": sv["inventoryItem"]["id"],
                        "tracked": sv["inventoryItem"].get("tracked"),
                        "label": f"{title} · {db_label.get(key, '?')}",
                        "raises_price": price_diff
                        and float(want_price) > float(have_price),
                        "price": (have_price, want_price) if price_diff else None,
                        "compare": (have_compare, want_compare) if compare_diff else None,
                        "qty": (have_qty, want_qty) if qty_diff else None,
                    }
                )

        for key in sorted(set(db_by_key) - shop_keys):
            plan["missing"].append(f"{title}: {db_label.get(key, '?')}")

    for pid in sorted(set(db_products) - seen_db_ids):
        plan["note"].append(
            f"{db_products[pid]['name']} ({pid}) is in wanas.db but has no Shopify product"
        )

    return plan, problems


def report(plan, problems):
    def head(t):
        print(f"\n{t}\n" + "-" * len(t))

    if problems:
        head("PROBLEMS (read these first)")
        for p in problems:
            print("  ! " + p)

    # A price going up is the one change a customer can feel, so it gets its
    # own section instead of being buried among dozens of stock corrections.
    rises = [v for v in plan["variants"] if v.get("raises_price")]
    if rises:
        head(f"PRICE INCREASES ({len(rises)}) — confirm these deliberately")
        for v in rises:
            print(f"  {v['label']}: {v['price'][0]} -> {v['price'][1]}")

    head(f"VARIANT FIXES ({len(plan['variants'])})")
    if not plan["variants"]:
        print("  nothing to change")
    for v in plan["variants"]:
        bits = []
        if v["price"]:
            bits.append(f"price {v['price'][0]} -> {v['price'][1]}")
        if v["compare"]:
            bits.append(f"compare-at {v['compare'][0] or '(none)'} -> {v['compare'][1] or '(none)'}")
        if v["qty"]:
            bits.append(f"stock {v['qty'][0]} -> {v['qty'][1]}")
        print(f"  {v['label']}: " + "; ".join(bits))

    head(f"PRODUCT TYPE FIXES ({len(plan['product_type'])})")
    for t in plan["product_type"] or []:
        print(f"  {t['title']}: {t['from']} -> {t['to']}")
    if not plan["product_type"]:
        print("  nothing to change")

    head(f"DESCRIPTIONS TO CLEAN ({len(plan['description'])})")
    for d in plan["description"] or []:
        print(f"  {d['title']}")
    if not plan["description"]:
        print("  nothing to change")

    head(f"VARIANTS MISSING ON SHOPIFY ({len(plan['missing'])})")
    for m in plan["missing"]:
        print("  " + m)
    if not plan["missing"]:
        print("  none")

    head("PRODUCTS MISSING ON SHOPIFY")
    for n in plan["note"]:
        print("  " + n)
    if not plan["note"]:
        print("  none")


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

VARIANTS_UPDATE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    userErrors { field message }
  }
}
"""

PRODUCT_UPDATE = """
mutation($input: ProductInput!) {
  productUpdate(input: $input) { userErrors { field message } }
}
"""

INVENTORY_SET = """
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) { userErrors { field message } }
}
"""


def apply_plan(gql: Shopify, plan, loc):
    # prices, grouped per product so each product is one call
    by_product = {}
    for v in plan["variants"]:
        if v["price"] or v["compare"]:
            entry = {"id": v["variant_id"]}
            if v["price"]:
                entry["price"] = v["price"][1]
            if v["compare"]:
                entry["compareAtPrice"] = v["compare"][1]
            by_product.setdefault(v["product_id"], []).append(entry)

    for pid, variants in by_product.items():
        gql(VARIANTS_UPDATE, {"productId": pid, "variants": variants})
        print(f"  prices updated: {len(variants)} variant(s)")

    # inventory
    quantities = [
        {
            "inventoryItemId": v["inventory_item_id"],
            "locationId": loc,
            "quantity": v["qty"][1],
        }
        for v in plan["variants"]
        if v["qty"]
    ]
    for i in range(0, len(quantities), 100):
        chunk = quantities[i : i + 100]
        gql(
            INVENTORY_SET,
            {
                "input": {
                    "name": "available",
                    "reason": "correction",
                    "ignoreCompareQuantity": True,
                    "quantities": chunk,
                }
            },
        )
        print(f"  inventory updated: {len(chunk)} variant(s)")

    for t in plan["product_type"]:
        gql(PRODUCT_UPDATE, {"input": {"id": t["id"], "productType": t["to"]}})
        print(f"  type: {t['title']} -> {t['to']}")

    for d in plan["description"]:
        gql(PRODUCT_UPDATE, {"input": {"id": d["id"], "descriptionHtml": d["to"]}})
        print(f"  description cleaned: {d['title']}")


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform the writes")
    args = ap.parse_args()

    env = load_env(ROOT / ".env")
    for key in ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN"):
        if not env.get(key):
            sys.exit(f"{key} is empty in .env")

    gql = Shopify(
        env["SHOPIFY_STORE_DOMAIN"],
        env["SHOPIFY_ADMIN_TOKEN"],
        env.get("SHOPIFY_API_VERSION", "2025-01"),
    )

    shop = gql("{shop{name currencyCode}}")["shop"]
    print(f"Connected to {shop['name']} ({shop['currencyCode']})")

    db_products, db_variants = read_db()
    print(f"wanas.db: {len(db_products)} products, {sum(len(v) for v in db_variants.values())} variants")

    shop_products = read_shopify(gql)
    print(f"Shopify:  {len(shop_products)} products, "
          f"{sum(len(p['variants']['nodes']) for p in shop_products)} variants")

    plan, problems = build_plan(shop_products, db_products, db_variants)
    report(plan, problems)

    # Exercise the write path's permissions now, in both modes, so a dry run
    # actually tells you whether --apply would succeed.
    loc, scope_error = probe_location(gql)
    needs_stock = any(v["qty"] for v in plan["variants"])

    print("\nWRITE PERMISSIONS\n" + "-" * 17)
    if scope_error:
        print("  prices, types, descriptions: ok")
        print("  inventory: BLOCKED\n")
        print("  " + scope_error.replace("\n", "\n  "))
    else:
        print("  prices, types, descriptions: ok")
        print("  inventory: ok")

    total = len(plan["variants"]) + len(plan["product_type"]) + len(plan["description"])
    if not args.apply:
        print(f"\nDRY RUN — nothing was written. {total} change(s) queued.")
        if scope_error and needs_stock:
            print("Fix the scope above before applying, or the stock half will fail.")
        else:
            print("Re-run with --apply once the diff above looks right.")
        return

    if not total:
        print("\nNothing to apply.")
        return

    # Refuse to start a partial run: applying prices while stock stays wrong
    # leaves the catalog in a state that looks corrected but isn't.
    if scope_error and needs_stock:
        sys.exit("\nNothing has been written — fix the scope above first.")

    print(f"\nApplying {total} change(s) to a live store...")
    apply_plan(gql, plan, loc)
    print("\nDone. Re-run without --apply to confirm the diff is now empty.")


if __name__ == "__main__":
    main()
