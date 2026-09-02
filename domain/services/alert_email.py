"""Which staff-queue items are worth waking the owner for, and what the email says.

The queue is the record; this is the tap on the shoulder that makes someone
open it. Three things the owner asked to hear about the moment they happen:

* a **negative or complaining Instagram comment** -- public, and getting worse
  the longer it sits;
* the **bot failing** -- a crashed turn, a reply Meta refused, a classifier or
  a token that stopped working;
* the **bot handing a conversation to a person** (`request_human`), which
  *pauses* that conversation: nobody is answering that customer until a staff
  member opens the dashboard.

Everything else the queue holds is deliberately silent. `order_confirmed` and
`low_stock` arrive by the dozen on a good day, and an address that also
carries them is an address the owner filters -- which costs exactly the three
above. `MAILED_ALERT_REASONS` is that line, and adding to it is a decision
about the owner's attention, not a formatting change.

**Domain does not send.** Like `notifications.register_transcript_recorder`,
the mailer arrives as a registered port (`app.py` wires
`integrations/mail/client.py`), so this module keeps no vendor import and the
test suite substitutes a list.

**And domain does not send *during* a transaction.** The mail is queued on
`common.events.after_commit` and handed to a daemon thread: an alert about an
order that later rolled back is a lie, and a two-second SMTP round trip on the
order path is a two-second stall for the customer.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from common.events import after_commit
from domain.models import QueueKind

log = logging.getLogger("wanas.alert_email")

#: The alert reasons that reach the owner's inbox. Every one of them is
#: something a person has to *do* something about; nothing routine is here.
MAILED_ALERT_REASONS = frozenset(
    {
        # -- the public surface going wrong ------------------------------
        "negative_comment",
        "customer_complaint",
        "comment_flood",
        # -- the bot not working ----------------------------------------
        "turn_crashed",
        "reply_delivery_failed",
        "instagram_reply_delivery_failed",
        "classifier_unavailable",
        "instagram_token_refresh_failed",
        # -- the bot working but unable to reach the customer ------------
        "confirmation_delivery_failed",
        "status_push_undelivered",
        "proactive_outreach_failed",
    }
)

#: Read as a subject-line prefix, so the owner can tell at a glance from the
#: phone's lock screen which of the three kinds this is.
_SUBJECT_PREFIX = {
    "negative_comment": "Negative comment",
    "customer_complaint": "Complaint",
    "comment_flood": "Comment flood",
    "turn_crashed": "Bot error",
    "reply_delivery_failed": "Undelivered reply",
    "instagram_reply_delivery_failed": "Undelivered reply",
    "classifier_unavailable": "Bot degraded",
    "instagram_token_refresh_failed": "Instagram token",
    "confirmation_delivery_failed": "Undelivered confirmation",
    "status_push_undelivered": "Undelivered order update",
    "proactive_outreach_failed": "Undelivered notice",
}

_Mailer = Callable[[str, str], bool]
_mailer: _Mailer | None = None


def register_mailer(fn: _Mailer) -> None:
    """The one place this module learns how to send. Called from `app.py`."""
    global _mailer
    _mailer = fn


@dataclass(frozen=True)
class _Snapshot:
    """The queue item's values, copied while the session is still open.

    Not the ORM object: the hook runs after commit, when reading an expired
    attribute would go back to a connection this code no longer owns.
    """

    queue_id: str
    kind: str
    reason: str
    summary: str
    channel: str
    external_id: str
    payload: dict


def should_mail(kind: str, reason: str | None) -> bool:
    """A handoff always -- it pauses a conversation, so a customer is sitting
    there unanswered until someone picks it up. An alert only if it is on the
    list above. An item swap never: it is a decision, not an emergency."""
    if kind == QueueKind.HANDOFF.value:
        return True
    if kind == QueueKind.ALERT.value:
        return (reason or "") in MAILED_ALERT_REASONS
    return False


# --------------------------------------------------------------------------
# Rate limiting. Both halves are per-process and in memory on purpose: a
# restart forgetting that it already mailed about something is the harmless
# direction, and a database table read on every alert is not worth it.
# --------------------------------------------------------------------------

_lock = threading.Lock()
#: (reason, external_id) -> when it was last mailed.
_last_sent: dict[tuple[str, str], float] = {}
#: The timestamps of every mail in the last hour, for the hard ceiling.
_recent: list[float] = []


def _allowed(snapshot: _Snapshot, *, cooldown: float, max_per_hour: int) -> bool:
    now = time.monotonic()
    key = (snapshot.reason, snapshot.external_id)
    with _lock:
        _recent[:] = [t for t in _recent if now - t < 3600]
        if max_per_hour > 0 and len(_recent) >= max_per_hour:
            log.warning(
                "alert email ceiling reached (%s/hour); %s not mailed",
                max_per_hour,
                snapshot.queue_id,
            )
            return False
        previous = _last_sent.get(key)
        if previous is not None and now - previous < cooldown:
            log.info(
                "alert email for %s suppressed: %r about %s was mailed %.0fs ago",
                snapshot.queue_id,
                snapshot.reason,
                snapshot.external_id or "-",
                now - previous,
            )
            return False
        _last_sent[key] = now
        _recent.append(now)
    return True


def reset_rate_limit() -> None:
    """For tests, which run many alerts through in one process."""
    with _lock:
        _last_sent.clear()
        _recent.clear()


def _compose(snapshot: _Snapshot) -> tuple[str, str]:
    from config.settings import settings

    if snapshot.kind == QueueKind.HANDOFF.value:
        prefix = "Bot handed over"
    else:
        prefix = _SUBJECT_PREFIX.get(snapshot.reason, "Alert")
    where = f" ({snapshot.channel})" if snapshot.channel else ""
    subject = f"[Wanas] {prefix}{where}: {snapshot.summary[:80]}"

    lines = [
        snapshot.summary,
        "",
        f"Queue item : {snapshot.queue_id} ({snapshot.kind})",
        f"Reason     : {snapshot.reason or '-'}",
        f"Channel    : {snapshot.channel or '-'}",
        f"Customer   : {snapshot.external_id or '-'}",
    ]
    if snapshot.kind == QueueKind.HANDOFF.value:
        lines += [
            "",
            "This conversation is PAUSED -- the bot will not answer this "
            "customer again until a staff member replies to it or resolves "
            "it in the dashboard.",
        ]
    for key, value in sorted((snapshot.payload or {}).items()):
        lines.append(f"{key:<11}: {str(value)[:500]}")
    base = settings.public_base_url
    if base:
        lines += ["", f"Open the dashboard: {base}/dashboard"]
    return subject, "\n".join(lines)


def _spawn(fn, snapshot: _Snapshot, **kwargs) -> None:
    """Run the send on a daemon thread of its own.

    Never the caller's: this hook fires on the order path, the webhook
    worker, and the scheduler tick, and none of them may sit waiting on a
    mail server. Its own function so the suite can run the send inline and
    assert on the result, without reaching into the threading module.
    """
    threading.Thread(
        target=fn,
        args=(snapshot,),
        kwargs=kwargs,
        name=f"alert-email-{snapshot.queue_id}",
        daemon=True,
    ).start()


def _send(snapshot: _Snapshot, *, cooldown: float, max_per_hour: int) -> None:
    mailer = _mailer
    if mailer is None:
        return
    if not _allowed(snapshot, cooldown=cooldown, max_per_hour=max_per_hour):
        return
    subject, body = _compose(snapshot)
    try:
        mailer(subject, body)
    except Exception:
        # Belt and braces -- the client swallows its own failures too. An
        # unsendable email must never be able to surface anywhere near the
        # code that raised the alert.
        log.exception("alert email failed for %s", snapshot.queue_id)


def notify(session, item) -> None:
    """Queue an email about `item`, to go out once its transaction commits.

    Called by `queues.enqueue` for every queue item; the filtering is here so
    there is one answer to "does this reach the owner", in one file.
    """
    from config.settings import settings

    if _mailer is None or not should_mail(item.kind, item.reason):
        return
    snapshot = _Snapshot(
        queue_id=item.queue_id,
        kind=item.kind,
        reason=item.reason or "",
        summary=item.summary or "",
        channel=item.channel or "",
        external_id=item.external_id or "",
        payload=dict(item.payload or {}),
    )
    cooldown = settings.alert_email_cooldown_seconds
    max_per_hour = settings.alert_email_max_per_hour

    def _hook() -> None:
        _spawn(_send, snapshot, cooldown=cooldown, max_per_hour=max_per_hour)

    after_commit(session, _hook)
