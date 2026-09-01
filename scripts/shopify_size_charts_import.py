"""Read the size-chart metafields back out of Shopify into the database.

The other direction from `scripts/shopify_size_charts.py`. That one publishes
`data/size_charts.json` to `custom.size_chart` / `custom.size_chart_data` so
the storefront can show a chart; this one brings back what has been edited or
added in Shopify Admin since, so the bot quotes the same numbers the product
page does.

    python scripts/shopify_size_charts_import.py            # dry run
    python scripts/shopify_size_charts_import.py --apply    # write

Dry run by default, like every other script in here, and the dry run is the
supervision: it prints which product gets which chart, which charts Shopify
already agrees with, and which Shopify products match no local SKU. Read it
before applying.

Additive and idempotent. Nothing here deletes a chart, unlinks a product, or
clears a column: a product whose metafields are empty is left exactly as it
is, because absence in Shopify is not a statement that the local chart is
wrong. A chart Shopify agrees with is skipped rather than rewritten -- see
the module docstring on `integrations/shopify/size_chart_import.py` for why
a round trip of our own publish must not take `data/size_charts.json` out of
play.

`DATABASE_URL` must point at the same database the shop runs on. Matching a
Shopify product to a local one is by variant SKU.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domain.db import session_scope  # noqa: E402
from integrations.shopify import size_chart_import  # noqa: E402
from integrations.shopify.client import ShopifyConfigError, ShopifyUnavailable  # noqa: E402


def run(*, apply: bool) -> int:
    nodes = size_chart_import.iter_shopify_charts()
    published = [n for n in nodes if n.get("data") or n.get("image_url")]
    print(f"Shopify products: {len(nodes)}, of them carrying a chart: {len(published)}")

    with session_scope() as db:
        plan = size_chart_import.build_plan(db, nodes)

        for node in plan["unmatched"]:
            print(f"  ! {node['title']}: no local product carries any of its SKUs")
        if plan["unchanged"]:
            print(f"  = {len(plan['unchanged'])} product(s) Shopify already agrees with, skipped")

        print()
        for entry in plan["products"]:
            if entry["chart"]:
                what = f"chart {entry['chart_id']} ({len(entry['chart']['sizes'])} sizes)"
                if entry["image_url"]:
                    what += " + diagram"
            else:
                what = "diagram only"
            print(f"  {entry['product_id']:28} <- {what}")
        print(f"\n{len(plan['products'])} product(s) to write, "
              f"{len(plan['charts'])} chart(s)")

        if not apply:
            print("\ndry run: nothing was written. Re-run with --apply.")
            return 0

        written = size_chart_import.apply_plan(db, plan)

    print(
        f"charts written: {written['charts']}, "
        f"products relinked: {written['linked']}, "
        f"chart pictures set: {written['product_images']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to the database")
    args = parser.parse_args()

    print("Importing size charts from Shopify")
    print(f"  mode: {'APPLY -- writing to the database' if args.apply else 'dry run'}")
    print()
    try:
        return run(apply=args.apply)
    except ShopifyConfigError as exc:
        print(f"Shopify is not configured: {exc}", file=sys.stderr)
        return 2
    except size_chart_import.EmptyRead as exc:
        print(f"Refusing to act: {exc}", file=sys.stderr)
        return 1
    except ShopifyUnavailable as exc:
        print(f"Shopify refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
