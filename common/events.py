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
_CLOSE_KEY = "after_close_hooks"


def after_commit(session: Session, fn) -> None:
    session.info.setdefault(_KEY, []).append(fn)


def after_close(session: Session, fn) -> None:
    """Queue work that needs a database of its own, once this one is done.

    An after-commit hook may send, but it must not write. The transaction has
    committed, yet the connection is still checked out and SQLite's write lock
    goes with it -- a second connection opened in there waits for a lock that
    is only released when the hook returns, which is to say never. That is why
    `domain/services/notifications.py` writes its transcript lines *inside*
    the transaction and only sends afterwards.

    Some things cannot be known in time to do that, though: whether Meta
    accepted the message is learned from the send itself, and "the customer
    never got this" has to be written down somewhere. So it is queued here and
    run by `domain/db.py::session_scope` once the session is closed and the
    lock is gone.

    Only a session managed by `session_scope` drains these. Anything else must
    do the work itself rather than queue it -- see `deliver_status_push`.
    """
    session.info.setdefault(_CLOSE_KEY, []).append(fn)


def run_after_close(session: Session) -> None:
    """Run and clear the queue above. Called by `session_scope` after
    `close()`, which is the point at which a new connection can get a write
    lock. Each hook is independent; one failing must not skip the rest."""
    for hook in session.info.pop(_CLOSE_KEY, []):
        try:
            hook()
        except Exception:
            log.exception("after-close hook failed")


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
    # Not the close queue: it is only ever filled *after* a commit, by a hook
    # describing something that has already left the building, and a rollback
    # of some later transaction on the same session says nothing about it.
