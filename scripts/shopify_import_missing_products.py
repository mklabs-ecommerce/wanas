"""Import Shopify products the bot cannot currently find.

A product created straight in Shopify Admin -- not through the dashboard's
own product-create panel -- never gets a row in wanas.db, and
`catalog.get_products` (the bot's search) only ever reads wanas.db. The
product exists, staff can see it in Shopify Admin, and the bot tells a
customer it does not exist. See `backend/services/shopify_product_import.py`
for the full explanation and the (deliberately narrow) scope.

Dry run by default, like every other script here. Read the report, then:

    python scripts/shopify_import_missing_products.py            # dry run
    python scripts/shopify_import_missing_products.py --apply    # perform it

Idempotent: a product already imported (or one that was never missing) is
skipped on every later run, so this is safe to run on a schedule.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.shopify_product_import import import_missing_products  # noqa: E402
from config.settings import settings  # noqa: E402
from domain.db import session_scope  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the import")
    args = parser.parse_args()

    if not settings.shopify_configured:
        sys.exit("SHOPIFY_STORE_DOMAIN / SHOPIFY_ADMIN_TOKEN missing from .env")

    with session_scope() as db:
        report = import_missing_products(db, apply=args.apply)

    print(f"Checked {report['checked']} active Shopify product(s).")

    if report["problems"]:
        print(f"\nPROBLEMS ({len(report['problems'])}) -- read these first")
        for p in report["problems"]:
            print("  ! " + p)

    if report["imported"]:
        verb = "Imported" if args.apply else "Would import"
        print(f"\n{verb} ({len(report['imported'])})")
        for item in report["imported"]:
            print(f"  + {item['title']} -> {item['product_id']} ({item['variants']} variant(s))")
    else:
        print("\nNothing missing -- every active Shopify product already has a wanas.db row.")

    if not args.apply and report["imported"]:
        print("\nDRY RUN -- nothing was written. Re-run with --apply once this looks right.")


if __name__ == "__main__":
    main()
