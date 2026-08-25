"""Sequential public identifiers.

`WNS-<n>` from 1001 for orders (AGENTS.md), `SWAP-<n>` for swap requests
(15-tool-contracts.md), `HO-<n>` and `ALERT-<n>` for the other two queue kinds
-- 16 only pins the swap prefix by example.

The counter row is updated in place inside the caller's transaction. A
`max(order_id) + 1` would hand two concurrent checkouts the same number.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from domain.models import Counter, QueueKind

ORDER_COUNTER = "order_id"
QUEUE_COUNTER = "queue_id"

_STARTS = {ORDER_COUNTER: 1000, QUEUE_COUNTER: 0}

_QUEUE_PREFIX = {
    QueueKind.ITEM_SWAP.value: "SWAP",
    QueueKind.HANDOFF.value: "HO",
    QueueKind.ALERT.value: "ALERT",
}


def _next(session: Session, name: str) -> int:
    row = session.get(Counter, name, with_for_update=True) if _supports_for_update(session) else session.get(
        Counter, name
    )
    if row is None:
        row = Counter(name=name, value=_seed_value(session, name))
        session.add(row)
        session.flush()
    # An UPDATE ... SET value = value + 1 keeps the increment in the database
    # rather than in Python, so it is safe even without row locking.
    session.execute(update(Counter).where(Counter.name == name).values(value=Counter.value + 1))
    session.flush()
    return session.scalar(select(Counter.value).where(Counter.name == name))


def _supports_for_update(session: Session) -> bool:
    return session.get_bind().dialect.name != "sqlite"


def _seed_value(session: Session, name: str) -> int:
    """Where a missing counter row restarts from.

    The default (`_STARTS`) is right for a fresh database and wrong for every
    other way one comes into existence: a dump restored without `counters`, a
    SQLite -> PostgreSQL move that copied the order tables, a counters table
    recreated by `create_all` next to orders that were already there. In all
    of those the counter restarts at 1000 while `WNS-1001` already exists, and
    the *next* `INSERT` fails on the primary key -- inside the order
    transaction, after Shopify has already created the sale and taken the
    stock. The savepoint rolls the increment back with everything else, so the
    counter never gets past the collision: every order from that point on is
    created on Shopify and then cancelled again. Reading the highest id that
    actually exists costs one query, once, and only when the row is missing.
    """
    if name != ORDER_COUNTER:
        return _STARTS.get(name, 0)

    from domain.models import Order

    highest = _STARTS[ORDER_COUNTER]
    for (order_id,) in session.execute(select(Order.order_id)).all():
        _, _, suffix = (order_id or "").partition("-")
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest


def next_order_id(session: Session) -> str:
    return f"WNS-{_next(session, ORDER_COUNTER)}"


def next_queue_id(session: Session, kind: str) -> str:
    prefix = _QUEUE_PREFIX.get(kind, "Q")
    return f"{prefix}-{_next(session, QUEUE_COUNTER)}"
