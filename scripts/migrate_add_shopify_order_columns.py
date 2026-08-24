"""Add `shopify_order_id` and `shopify_order_name` to the orders table.

The project creates its schema with `Base.metadata.create_all`, which builds
missing *tables* and ignores missing *columns*. A fresh database gets the new
fields automatically; `wanas.db`, which already exists and has orders in it,
does not. Hence this.

    python scripts/migrate_add_shopify_order_columns.py            # dry run
    python scripts/migrate_add_shopify_order_columns.py --apply

Idempotent, and additive only: two nullable columns. Existing orders keep both
as NULL, which is correct -- they were placed before the store had them, and
backfilling a guess would be worse than an honest blank.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "wanas.db"

COLUMNS = {
    "shopify_order_id": "VARCHAR(80)",
    "shopify_order_name": "VARCHAR(40)",
}


def existing_columns(con: sqlite3.Connection) -> set[str]:
    return {row[1] for row in con.execute("PRAGMA table_info(orders)")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the migration")
    parser.add_argument("--db", default=str(DB), help="path to the database")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        sys.exit(f"No database at {path}")

    con = sqlite3.connect(path)
    have = existing_columns(con)
    if not have:
        sys.exit("No `orders` table in that database.")

    missing = {name: ddl for name, ddl in COLUMNS.items() if name not in have}
    orders = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    print(f"database : {path}")
    print(f"orders   : {orders}")
    if not missing:
        print("\nNothing to do -- both columns already exist.")
        con.close()
        return

    print("\nWould add:")
    for name, ddl in missing.items():
        print(f"  + orders.{name} {ddl} NULL")
    print(f"\n{orders} existing orders keep NULL in both -- they predate the Shopify move.")

    if not args.apply:
        print("\nDry run -- nothing was written. Re-run with --apply.")
        con.close()
        return

    # A copy before touching a file that holds real orders. ALTER TABLE ADD
    # COLUMN is about as safe as SQLite gets, but "about as safe as" is not a
    # reason to have no way back.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.before-{stamp}{path.suffix}")
    shutil.copy2(path, backup)
    print(f"\nbackup: {backup.name}")

    for name, ddl in missing.items():
        con.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")
        print(f"  added orders.{name}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_orders_shopify_order_id ON orders (shopify_order_id)")
    con.commit()
    con.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
