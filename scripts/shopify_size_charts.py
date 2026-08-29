"""Publish the size charts to Shopify, so the product page can show them.

`data/size_charts.json` and `data/size-charts/*.png` have always been the
bot's. This puts the same charts on every matching Shopify product as two
metafields -- `custom.size_chart` (the diagram) and `custom.size_chart_data`
(the measurements, as JSON) -- which `theme/size-chart.liquid` renders as a
bilingual table on the storefront.

    python scripts/shopify_size_charts.py            # dry run
    python scripts/shopify_size_charts.py --apply    # upload and write

Dry run by default, like every other script in here, and the dry run is the
supervision: it prints which product gets which chart, and which products it
could not match. Read it before applying.

Idempotent, and safe to re-run after adding a product: a diagram already in
Shopify Files is reused rather than uploaded again, an existing metafield
definition is left alone, and a metafield is overwritten with the same value.
Nothing is ever deleted.

Which product gets which chart comes from `Product.size_chart` in the
database, so `DATABASE_URL` must point at the same database the shop runs on.
Matching that product to Shopify's is by variant SKU.

Uploading a diagram is a file write, which `write_products` already covers on
this shop's token. If a store's token does not, the run stops and names the
`read_files` / `write_files` scopes rather than failing as an outage.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domain.db import session_scope  # noqa: E402
from integrations.shopify import size_charts  # noqa: E402
from integrations.shopify.client import ShopifyConfigError, ShopifyUnavailable  # noqa: E402


def run(*, apply: bool, replace_images: bool = False) -> int:
    charts = size_charts.load_charts()
    print(f"charts on disk: {len(charts)}")

    missing_art = [cid for cid, c in charts.items() if size_charts.chart_image(c) is None]
    for chart_id in missing_art:
        print(f"  ! {chart_id}: no diagram file -- the table goes up without a picture")

    created = size_charts.ensure_definitions(apply=apply)
    verb = "created" if apply else "to create"
    print(f"metafield definitions {verb}: {', '.join(created) if created else 'none, already there'}")

    with_art = len(charts) - len(missing_art)
    files = size_charts.ensure_files(charts, apply=apply)
    if apply:
        print(f"diagrams in Shopify Files: {len(files)} of {with_art}")
    else:
        print(f"diagrams already in Shopify Files: {len(files)} of {with_art} "
              f"({with_art - len(files)} to upload)")

    with session_scope() as db:
        plan = size_charts.build_plan(db, charts, files, replace_images=replace_images)

    for chart_id in plan["unknown_charts"]:
        print(f"  ! products name chart {chart_id!r}, which is not in size_charts.json")
    for product in plan["unmatched"]:
        print(f"  ! {product['product_id']}: no Shopify product carries any of its SKUs")

    print()
    for entry in plan["entries"]:
        if entry["kept_existing_image"]:
            art = "table only, keeping the diagram already set in Admin"
        elif entry["file_gid"]:
            art = "diagram + table"
        else:
            art = "table only"
        print(f"  {entry['product_id']:28} -> {entry['chart_id']:22} ({art})")
    print(f"\n{len(plan['entries'])} product(s) to write")

    if not apply:
        print("\ndry run: nothing was written. Re-run with --apply.")
        return 0

    written = size_charts.write_plan(plan["entries"])
    print(f"metafields written: {written}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="upload and write")
    parser.add_argument(
        "--replace-images",
        action="store_true",
        help="also overwrite a diagram somebody set by hand in Shopify Admin",
    )
    args = parser.parse_args()

    print("Publishing size charts to Shopify")
    print(f"  mode: {'APPLY -- writing to Shopify' if args.apply else 'dry run'}")
    print()
    try:
        return run(apply=args.apply, replace_images=args.replace_images)
    except ShopifyConfigError as exc:
        print(f"Shopify is not configured: {exc}", file=sys.stderr)
        return 2
    except (size_charts.SizeChartError, ShopifyUnavailable) as exc:
        print(f"Shopify refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
