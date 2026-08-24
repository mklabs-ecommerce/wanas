"""Post-commit hooks.

An outbound message must never describe a database write that later rolled
back. "Your order is confirmed" for an order that does not exist is the worst
failure available here, so anything that leaves the building is queued on the
session and fired only once the transaction has actually committed.

The alert *row* is written inside the transaction (it is part of the order);
only the network call waits.
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

log = logging.getLogger("wanas.events")

_KEY = "after_commit_hooks"


def after_commit(session: Session, fn) -> None:
    session.info.setdefault(_KEY, []).append(fn)


@event.listens_for(Session, "after_commit")
def _run_after_commit(session: Session) -> None:
    hooks = session.info.pop(_KEY, [])
    for hook in hooks:
        try:
            hook()
        except Exception:  # a failed send must not undo a committed order
            log.exception("after-commit hook failed")


@event.listens_for(Session, "after_rollback")
def _drop_after_rollback(session: Session) -> None:
    session.info.pop(_KEY, None)
