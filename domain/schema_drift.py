"""What the models declare, minus what the database actually has.

`Base.metadata.create_all` builds missing *tables* and silently ignores missing
*columns*. That is fine for a fresh database and wrong for every one that has
been running: add a column to a model, deploy, and the table keeps the shape it
was created with. Nothing complains at boot -- the failure surfaces at the first
`INSERT` that mentions the new column, which for `orders.source_external_id`
meant every order in production was created on Shopify and then cancelled again,
for four days, with a healthy-looking service.

So the comparison is done explicitly, at startup (`app.py`, which logs it) and
from `scripts/migrate_schema.py`, which can add what is missing.

Additive only, and deliberately so. A column the models no longer declare is
reported and never dropped -- an old deploy may still be serving traffic against
it, and a dropped column takes its data with it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Column, Engine, inspect, text
from sqlalchemy.schema import CreateColumn

from domain.models import Base

log = logging.getLogger("wanas.schema")


@dataclass
class Drift:
    """Everything the database is missing, per table."""

    missing_tables: list[str] = field(default_factory=list)
    #: table -> the Column objects the models declare and the table lacks.
    missing_columns: dict[str, list[Column]] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.missing_tables and not self.missing_columns

    def describe(self) -> list[str]:
        out = [f"table {name} is missing entirely" for name in self.missing_tables]
        for table, columns in self.missing_columns.items():
            for column in columns:
                out.append(f"{table}.{column.name} ({column.type}) is missing")
        return out


def detect(engine: Engine) -> Drift:
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    drift = Drift()

    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            drift.missing_tables.append(table.name)
            continue
        have = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [column for column in table.columns if column.name not in have]
        if missing:
            drift.missing_columns[table.name] = missing
    return drift


def addable(column: Column) -> bool:
    """Whether this column can be added to a table that already has rows.

    A `NOT NULL` column with no server default cannot: the database has to put
    something in it for every existing row and there is nothing to put. Those
    are reported and left alone rather than added as nullable behind the
    author's back -- guessing at the shape of someone's schema is how a
    migration quietly diverges from the model it was meant to match.
    """
    return column.nullable or column.server_default is not None


def add_column_sql(engine: Engine, table: str, column: Column) -> str:
    """`ALTER TABLE ... ADD COLUMN`, in this database's own dialect."""
    spec = CreateColumn(column).compile(engine).string.strip()
    return f"ALTER TABLE {table} ADD COLUMN {spec}"


def apply_additive(engine: Engine) -> list[str]:
    """Add every missing column that can be added. Returns what it ran.

    Additive, idempotent and dialect-native, which is what makes it safe to run
    on every boot -- the same guarantee `_ensure_catalog_seeded` and
    `_ensure_shipping_fees_set` already give in `app.py`. Nothing here drops,
    retypes or backfills anything.

    Each column is its own statement and its own failure: two replicas booting
    together means one of them loses the race and sees "column already exists",
    which is a no-op, not a reason to leave the other columns unadded.
    """
    drift = detect(engine)
    ran: list[str] = []
    for table, columns in drift.missing_columns.items():
        for column in columns:
            if not addable(column):
                log.error(
                    "SCHEMA DRIFT: %s.%s is NOT NULL with no default and cannot be added to a "
                    "table that already has rows -- add it by hand, with the value the existing "
                    "rows should carry",
                    table,
                    column.name,
                )
                continue
            statement = add_column_sql(engine, table, column)
            try:
                with engine.begin() as conn:
                    conn.execute(text(statement))
            except Exception:
                # Already added by another replica, or a column this cannot
                # write. Either way the re-check below is what decides.
                log.exception("could not add %s.%s", table, column.name)
                continue
            log.warning("SCHEMA: added missing column %s.%s (%s)", table, column.name, column.type)
            ran.append(statement)
    return ran


def log_drift(engine: Engine) -> Drift:
    """Say loudly, at boot, what the running schema is missing.

    An ERROR rather than a warning: a column the code writes and the table does
    not have is not a degraded feature, it is every write to that table failing.
    """
    try:
        drift = detect(engine)
    except Exception:  # a broken inspection must not stop the app booting
        log.exception("could not compare the database schema against the models")
        return Drift()

    if drift.clean:
        return drift

    for line in drift.describe():
        log.error("SCHEMA DRIFT: %s", line)
    log.error(
        "The database is missing %d column(s) and %d table(s) the code expects. "
        "`create_all` does not add columns to existing tables -- run "
        "`python scripts/migrate_schema.py --apply` against this database. Until "
        "then, every write that mentions a missing column fails.",
        sum(len(cols) for cols in drift.missing_columns.values()),
        len(drift.missing_tables),
    )
    return drift
