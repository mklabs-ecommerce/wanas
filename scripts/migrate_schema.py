"""Add the columns the models declare and the database does not have.

The schema is created with `Base.metadata.create_all`, which adds missing
*tables* and ignores missing *columns*. Every database that predates a model
change therefore keeps its old shape, and the first write that mentions a new
column fails -- in production that was `orders.source_external_id`, which made
every order create itself on Shopify and then cancel again.

    python scripts/migrate_schema.py                    # dry run, DATABASE_URL
    python scripts/migrate_schema.py --apply
    python scripts/migrate_schema.py --database-url postgresql+psycopg://...

Additive and idempotent: it only ever issues `ALTER TABLE ... ADD COLUMN`, in
the database's own dialect, for columns that can be added to a populated table
(nullable, or carrying a server default). It creates missing tables through
`create_all`. It never drops or alters an existing column, and it never touches
data.
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import create_engine, text

from domain.db import normalise_database_url
from domain.models import Base
from domain.schema_drift import add_column_sql, addable, detect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the migration")
    parser.add_argument(
        "--database-url",
        default=None,
        help="the database to migrate; defaults to DATABASE_URL / config.settings",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.database_url:
        url = normalise_database_url(args.database_url)
        engine = create_engine(url, future=True, pool_pre_ping=True)
    else:
        from domain.db import engine  # honours the deployed-SQLite guard

    # The scheme only -- a URL can carry credentials and this prints.
    print(f"database : {engine.dialect.name}")

    drift = detect(engine)
    if drift.clean:
        print("\nNothing to do -- the schema matches the models.")
        return 0

    if drift.missing_tables:
        print("\nMissing tables:")
        for name in drift.missing_tables:
            print(f"  + {name}")

    to_add: list[tuple[str, object]] = []
    blocked: list[str] = []
    for table, columns in drift.missing_columns.items():
        for column in columns:
            if addable(column):
                to_add.append((table, column))
            else:
                blocked.append(f"{table}.{column.name} ({column.type}) NOT NULL with no default")

    if to_add:
        print("\nWould add:")
        for table, column in to_add:
            print(f"  + {add_column_sql(engine, table, column)}")
    if blocked:
        print("\nCannot add automatically (every existing row would need a value):")
        for line in blocked:
            print(f"  ! {line}")
        print("  Add these by hand, with the value the existing rows should carry.")

    if not args.apply:
        print("\nDry run -- nothing was written. Re-run with --apply.")
        return 1 if blocked else 0

    if drift.missing_tables:
        Base.metadata.create_all(engine)
        print(f"\ncreated {len(drift.missing_tables)} missing table(s)")

    with engine.begin() as conn:
        for table, column in to_add:
            conn.execute(text(add_column_sql(engine, table, column)))
            print(f"  added {table}.{column.name}")

    remaining = detect(engine)
    if remaining.clean:
        print("\nDone -- the schema now matches the models.")
        return 0
    print("\nStill missing after the migration:")
    for line in remaining.describe():
        print(f"  ! {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
