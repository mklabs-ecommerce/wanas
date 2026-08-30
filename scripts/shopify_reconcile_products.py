"""Delete the wanas.db products whose Shopify products are gone.

The counterpart to the reconcile-on-boot in
`integrations/shopify/product_import.py`, which only ever *adds*. A local
product whose SKUs Shopify no longer knows gets no live price or stock, falls
back to the seeded columns, and is still offered to customers -- in
production that was three phantom "One Size" t-shirts at 0 EGP.

Dry run by default, like every other `scripts/shopify_*.py`. Read the report
before passing --apply: it is the only cheap moment to catch a bad read.

    python scripts/shopify_reconcile_products.py            # report only
    python scripts/shopify_reconcile_products.py --apply    # do it
    python scripts/shopify_reconcile_products.py --apply --force

--force is only for getting past the "more than half the catalog looks gone"
refusal, and you should have checked SHOPIFY_STORE_DOMAIN and the token
before you reach for it. See the module docstring on
`integrations/shopify/product_reconcile.py` for every rule this follows.

A product that has ever been ordered is archived rather than deleted: the
order lines are the record that money changed hands, and they still read.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.db import session_scope  # noqa: E402
from integrations.shopify.product_reconcile import (  # noqa: E402
    ReconcileRefused,
    reconcile_vanished_products,
)


def _show(label: str, rows: list[dict]) -> None:
    print(f"\n{label} ({len(rows)}):")
    if not rows:
        print("  (none)")
        return
    for row in rows:
        extra = f"  -- {row['why']}" if row.get("why") else ""
        skus = ", ".join(row.get("skus") or []) or "no variants"
        print(f"  {row['product_id']:40} {row.get('name') or ''}{extra}")
        print(f"      {skus}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the writes")
    parser.add_argument(
        "--force", action="store_true",
        help="proceed even if most of the catalog looks gone (check the store first)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        with session_scope() as session:
            report = reconcile_vanished_products(session, apply=args.apply, force=args.force)
    except ReconcileRefused as exc:
        print(f"refused: {exc}")
        return 2

    print(f"checked {report['checked']} local products against {report['live_skus']} live SKUs")
    _show("deleted" if args.apply else "would delete", report["deleted"])
    _show("archived (sold before)" if args.apply else "would archive (sold before)",
          report["archived"])
    _show("skipped", report["skipped"])
    if not args.apply:
        print("\nDry run -- nothing was written. Re-run with --apply to do it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
