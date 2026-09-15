"""Turn tax off for every variant already on Shopify.

This shop charges no tax -- the price quoted in a conversation is the price
charged. `orderCreate` was never sent `taxLines`, so a *new* order never
carried any; but `orderEditAddVariant` runs Shopify's own tax engine, and it
reads the *variant's own* `taxable` flag, which defaulted on for every
product ever created. That is what put an unquoted GST line on orders #1039
(112.00) and #1040 (81.20): the added variant was still marked taxable, so
the edit priced it as if the storefront did charge tax. `taxable: False` on
`orderCreate`'s own line items (see `integrations/shopify/orders.py::_line`)
closes the create-time half; this closes the other half, at the source, so
neither a future edit nor the storefront checkout can tax a variant again.

    python scripts/shopify_untax_products.py            # dry run, writes nothing
    python scripts/shopify_untax_products.py --apply    # perform the writes

Idempotent: a variant already `taxable: false` is skipped, so re-running
after adding products only touches the new ones. Refuses to run against an
empty or near-empty live read -- that is a bad token or a wrong store, not an
untaxed catalog. Stdlib only, like its siblings.

This does not touch orders #1039 / #1040 themselves, and it does not touch
Settings -> Taxes and duties in the admin -- see docs/OPERATIONS.md for both.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Same minimal stdlib client and .env loader every scripts/shopify_*.py uses,
# imported rather than copied so the three cannot drift apart.
from scripts.shopify_set_skus import Shopify  # noqa: E402
from scripts.shopify_sync import load_env  # noqa: E402

PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      variants(first: 100) {
        nodes { id sku taxable }
      }
    }
  }
}
"""

#: Up to 250 variants for one product per call (Shopify's own limit), which
#: makes this a handful of calls rather than one per variant.
UNTAX_MUTATION = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id taxable }
    userErrors { field message }
  }
}
"""

#: Below this many live products, a read is more likely a wrong store or a
#: near-empty catalog than the truth -- the same shape of guard
#: shopify_reconcile_products.py uses before it will delete anything.
MIN_EXPECTED_PRODUCTS = 5


def read_shopify(shop: Shopify) -> list[dict]:
    out, cursor = [], None
    while True:
        data = shop(PRODUCTS_QUERY, {"cursor": cursor})
        block = data["products"]
        out.extend(block["nodes"])
        if not block["pageInfo"]["hasNextPage"]:
            return out
        cursor = block["pageInfo"]["endCursor"]


def build_plan(shop_products: list[dict]) -> tuple[list[dict], int]:
    """Returns (plan, already_untaxed). Talks to nothing."""
    plan: list[dict] = []
    already = 0
    for sp in shop_products:
        updates = []
        for sv in sp["variants"]["nodes"]:
            if sv["taxable"] is False:
                already += 1
                continue
            updates.append({"id": sv["id"], "sku": sv.get("sku") or ""})
        if updates:
            plan.append({"product_id": sp["id"], "title": sp["title"], "updates": updates})
    return plan, already


def report(plan: list[dict], already: int) -> None:
    def head(t: str) -> None:
        print(f"\n{t}\n" + "-" * len(t))

    total = sum(len(p["updates"]) for p in plan)
    if plan:
        head(f"VARIANTS TO UNTAX ({total})")
        for p in plan:
            print(f"  {p['title']}")
            for u in p["updates"]:
                print(f"    {u['sku'] or u['id']}")

    head("SUMMARY")
    print(f"  already taxable:false : {already}")
    print(f"  to untax               : {total}")


def apply(shop: Shopify, plan: list[dict]) -> None:
    done = 0
    for p in plan:
        variants = [{"id": u["id"], "taxable": False} for u in p["updates"]]
        shop(UNTAX_MUTATION, {"productId": p["product_id"], "variants": variants})
        done += len(variants)
        print(f"  untaxed {len(variants):>3} variants on {p['title']}")
    print(f"\nDone: {done} variants set to taxable:false.")


def main() -> None:
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

    shop_products = read_shopify(shop)
    if len(shop_products) < MIN_EXPECTED_PRODUCTS:
        sys.exit(
            f"Only {len(shop_products)} products came back from Shopify -- refusing to "
            "touch tax settings on what looks like an empty or near-empty read. Check "
            "SHOPIFY_STORE_DOMAIN and the token."
        )

    plan, already = build_plan(shop_products)
    report(plan, already)

    if not args.apply:
        print("\nDry run -- nothing was written. Re-run with --apply once this looks right.")
        return

    if not plan:
        print("\nNothing to do -- every variant is already taxable:false.")
        return

    print()
    apply(shop, plan)


if __name__ == "__main__":
    main()
