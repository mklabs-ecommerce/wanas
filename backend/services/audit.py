"""Audit log.

With a single dashboard role, attribution is the only control there is.
"Who dropped that price to 5 EGP" has to be answerable.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import AuditLog


def record(
    session: Session,
    *,
    staff_id: int | None,
    action: str,
    entity: str,
    entity_id: str,
    before: dict | None = None,
    after: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        staff_id=staff_id,
        action=action,
        entity=entity,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )
    session.add(entry)
    session.flush()
    return entry


def recent(session: Session, limit: int = 100) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).order_by(AuditLog.at.desc()).limit(limit)).all())


def for_entity(session: Session, entity: str, entity_id: str) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity == entity, AuditLog.entity_id == str(entity_id))
            .order_by(AuditLog.at.desc())
        ).all()
    )
