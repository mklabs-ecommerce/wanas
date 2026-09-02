"""The staff queue -- swaps, handoffs and alerts in one table.

Three review queues with one `kind` rather than three tables: they share every
field that matters and staff work them from one list.

This inbox is where staff *work* an item, but it is no longer the only place
they learn one exists: `enqueue` also offers every item to
`domain/services/alert_email.py`, which mails the owner about the few that
cannot wait for somebody to open a browser tab. The filter lives there, not
here -- this function stays the one way a queue item is created.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import QueueStatus, StaffQueueItem, utcnow
from domain.services import alert_email
from domain.services.ids import next_queue_id


def enqueue(
    session: Session,
    *,
    kind: str,
    summary: str,
    reason: str | None = None,
    channel: str | None = None,
    external_id: str | None = None,
    order_id: str | None = None,
    payload: dict | None = None,
) -> StaffQueueItem:
    item = StaffQueueItem(
        queue_id=next_queue_id(session, kind),
        kind=kind,
        status=QueueStatus.OPEN.value,
        channel=channel,
        external_id=external_id,
        order_id=order_id,
        reason=reason,
        summary=summary or "",
        payload=payload or {},
    )
    session.add(item)
    session.flush()
    # After the flush so the item has its id, and inside the transaction so
    # the email is *queued* with the row -- it is only sent once that
    # transaction commits, and never from this thread. See alert_email.notify.
    alert_email.notify(session, item)
    return item


def resolve(
    session: Session, queue_id: str, staff_id: int, *, status: str = QueueStatus.RESOLVED.value
) -> StaffQueueItem | None:
    """Whichever staff action lands first wins.

    The status check is part of the same transaction as the write, so the
    second staff member sees "already resolved" rather than double-applying.
    """
    item = session.get(StaffQueueItem, queue_id)
    if item is None or item.status != QueueStatus.OPEN.value:
        return None
    item.status = status
    item.resolved_at = utcnow()
    item.resolved_by = staff_id
    session.flush()
    return item


def open_items(session: Session, kind: str | None = None) -> list[StaffQueueItem]:
    stmt = select(StaffQueueItem).where(StaffQueueItem.status == QueueStatus.OPEN.value)
    if kind:
        stmt = stmt.where(StaffQueueItem.kind == kind)
    return list(session.scalars(stmt.order_by(StaffQueueItem.created_at.desc())).all())

