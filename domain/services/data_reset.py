"""Wipe the bot's own record of customers, conversations and sales.

For starting the dashboard over from zero -- after testing, before a launch --
without touching anything the shop is *configured* with. `manage.py
reset-data --confirm` is the only caller, and it refuses without the flag.

Wiped: every conversation and message (`sessions`, `channel_identities`),
every customer (`clients`), every bot order and what hangs off one (items,
feedback, the review queue), carts, the stock waitlist, cart nudges, the
Instagram comment log, and the counters, so the next order is `RHL-1001`
and the next queue item number 1 again.

Kept, deliberately: the catalogue (`products`, `variants`, `size_charts`),
shipping fees (`shipping_rates`), settings and flags (`runtime_settings`,
`test_phone_numbers`), staff accounts, WhatsApp/Instagram configuration
(`whatsapp_media`, `integration_tokens`), and `webhook_events` -- that table
is Meta's redelivery guard, not data anyone reads, and emptying it would let
a late redelivery of an old message be answered a second time.

Shopify is never called. Its orders and customers are its own, and the
dashboard's store-wide sales figures are read live from Shopify -- this does
not, and must not, change them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.orm import Session

from domain.models import (
    AbandonedCartNudge,
    CartItem,
    ChannelIdentity,
    Client,
    Counter,
    InstagramCommentReply,
    Order,
    OrderFeedback,
    OrderItem,
    SessionRow,
    StaffQueueItem,
    StockWaitlistEntry,
)

#: Children before parents, so no foreign key is ever left dangling mid-way.
WIPED = (
    OrderFeedback,
    StaffQueueItem,
    OrderItem,
    Order,
    ChannelIdentity,
    Client,
    CartItem,
    StockWaitlistEntry,
    AbandonedCartNudge,
    InstagramCommentReply,
    SessionRow,
    Counter,
)


def counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
        for model in WIPED
    }


def reset(session: Session) -> dict[str, int]:
    """Delete every row in `WIPED`, in one transaction. Returns what each
    table held before."""
    before = counts(session)
    for model in WIPED:
        session.execute(delete(model))
    session.flush()
    return before


def _plain(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value if isinstance(value, (str, int, float, bool, list, dict, type(None))) else str(value)


def dump(session: Session, directory: Path) -> Path:
    """Every table, every row, as JSON -- the backup taken before a reset.

    Not a `pg_dump` (that needs the client tools wherever this runs); a
    readable, restorable copy of the same rows that works against SQLite and
    PostgreSQL alike. Written outside the repository by the caller's choice.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"rehla-backup-{stamp}.json"
    # Read what the live database holds, not what the models declare: a
    # database a deploy behind (or ahead) of the code must still back up.
    tables = {}
    live = inspect(session.get_bind())
    for name in live.get_table_names():
        quoted = session.get_bind().dialect.identifier_preparer.quote(name)
        rows = session.execute(text(f"SELECT * FROM {quoted}")).mappings().all()
        tables[name] = [{k: _plain(v) for k, v in row.items()} for row in rows]
    target.write_text(json.dumps({"taken_at": stamp, "tables": tables}, ensure_ascii=False), encoding="utf-8")
    return target
