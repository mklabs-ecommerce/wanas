"""Which staff-queue items are worth waking the owner for, and what the email says.

The queue is the record; this is the tap on the shoulder that makes someone
open it. Three things the owner asked to hear about the moment they happen:

* a **negative or complaining Instagram comment** -- public, and getting worse
  the longer it sits;
* the **bot failing** -- a crashed turn, a reply Meta refused, a classifier or
  a token that stopped working;
* the **bot handing a conversation to a person** (`request_human`), which
  *pauses* that conversation: nobody is answering that customer until a staff
  member opens the dashboard;
* an **order changing after it was placed** -- modified, cancelled, or an item
  the customer wants swapped -- because each of those is money and stock
  moving without anyone having decided it should;
* **stock running out**, which is the one alert that is cheaper to act on
  early than late.

One thing the queue holds is deliberately silent, and it is the loud one:
`order_confirmed`. It fires on every successful sale, which is the outcome
this whole system exists to produce -- an address carrying it is an address
the owner filters, and filtering it costs every alert above. That is the line
`MAILED_ALERT_REASONS` draws. Moving it is a decision about the owner's
attention, not a formatting change: the test that a reason belongs here is
"would the owner want to be interrupted for this?", and for a confirmed order
the answer is no precisely because there will be so many of them.

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
        # A customer who asked for something, was told the team would
        # confirm, and cannot now be told what the team decided.
        "resolution_undelivered",
        # -- an order changing after the fact, and the shelf running out ---
        # Not "the bot went wrong" like the two groups above; these are the
        # ordinary business events the owner asked to see anyway, because
        # each one is stock or money moving.
        "order_modified",
        "order_cancelled",
        "low_stock",
        # -- a public question the DM budget could not answer ------------
        # `order_status_comment` is raised when someone asks about their
        # order in a public comment. It used to be in no list at all: raised
        # in `assistant/channels/instagram.py`, declared nowhere, and
        # therefore silent -- which is the whole reason the decision table
        # below exists. It belongs in the inbox because it is
        # `public_reply_without_dm`: when the DM budget is spent the customer
        # gets a public line and *no answer*, and a person has to finish it.
        "order_status_comment",
        # -- a post-order request waiting on a person --------------------
        # These two ride on their own kinds (`_ALWAYS_MAILED_KINDS`), so
        # they never reach the reason test in practice. They are stated
        # anyway: they are declared alert reasons, and a reason that is
        # mailed in fact but undecided on paper is exactly the ambiguity the
        # rest of this table exists to remove.
        "swap_requested",
        "add_requested",
    }
)

#: And the reasons deliberately kept out of the inbox, each beside the reason
#: why. Silence is a decision, so it is written down rather than left to the
#: absence of an entry above -- an alert nobody is told about is the most
#: expensive kind of bug this file can have, and "nobody added it to the set"
#: and "somebody decided against it" used to look identical.
SILENT_ALERT_REASONS = {
    "order_confirmed": (
        "fires on every successful sale, which is the outcome this whole "
        "system exists to produce. An address carrying it is an address the "
        "owner filters, and filtering it costs every alert above."
    ),
    "spam_comment": (
        "the shop's own response to spam is to do nothing -- there is no "
        "decision for a person to make, and a misclassified customer is "
        "caught by `negative_comment`/`customer_complaint` instead."
    ),
}

#: Every reason this file has an opinion about. A reason that reaches
#: `should_mail` and is in neither half is a reason nobody decided about, and
#: it is mailed *and* logged rather than dropped -- see `should_mail`.
#: `tests/test_alert_email.py` fails if any reason raised anywhere in the
#: codebase is missing from here, so adding a queue reason cannot silently
#: skip the inbox.
ALERT_REASON_DECISIONS = frozenset(MAILED_ALERT_REASONS | set(SILENT_ALERT_REASONS))

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
    "resolution_undelivered": "Customer never told the outcome",
    "proactive_outreach_failed": "Undelivered notice",
    "order_modified": "Order changed",
    "order_cancelled": "Order cancelled",
    "low_stock": "Low stock",
    "swap_requested": "Item swap requested",
    "add_requested": "Item add requested",
    "order_status_comment": "Order-status question",
}

_Mailer = Callable[[str, str], bool]
_Describer = Callable[[], str]
_mailer: _Mailer | None = None
_describe: _Describer | None = None


def register_mailer(fn: _Mailer, describe: _Describer | None = None) -> None:
    """The one place this module learns how to send. Called from `app.py`.

    `describe` is the same port one step further: when a send is refused this
    module has to say *why* in the log, and the only thing that knows why is
    the transport. It arrives as a callable for the same reason the mailer
    does -- domain/ holds no vendor import, so it cannot go and ask.
    """
    global _mailer, _describe
    _mailer = fn
    _describe = describe


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
    order_id: str
    payload: dict

    @property
    def subject_key(self) -> str:
        """*What* this alert is about, for the cooldown to key on.

        A conversation-shaped alert carries an `external_id` and nothing else
        is needed. But the three order/stock reasons carry none -- they are
        raised about an order or a variant, not about whoever happened to be
        typing -- so keying the cooldown on `external_id` alone collapsed
        them all onto one empty key, and two different products going low
        inside the window meant the second one was never mailed. Falling back
        to the order and then the variant is what keeps "the same thing again"
        apart from "a second thing".
        """
        return (
            self.external_id
            or self.order_id
            or str(self.payload.get("variant_id") or "")
            or self.queue_id
        )


#: The kinds that are mailed whole, whatever their reason. Each one is a
#: customer who is *waiting*: a handoff pauses the conversation outright, and
#: a swap or an add sits there until a person presses a button. Reason-level
#: filtering would be the wrong tool -- there is no version of these that is
#: routine.
_ALWAYS_MAILED_KINDS = frozenset(
    {
        QueueKind.HANDOFF.value,
        QueueKind.ITEM_SWAP.value,
        QueueKind.ITEM_ADD.value,
    }
)


def should_mail(kind: str, reason: str | None) -> bool:
    """Whether this queue item wakes the owner.

    Three kinds are mailed whole (`_ALWAYS_MAILED_KINDS`); an alert is mailed
    if its reason says so.

    **An undecided reason is mailed, not dropped.** It used to be
    `reason in MAILED_ALERT_REASONS`, so a reason nobody had added -- which is
    what `order_status_comment` was, raised in production and listed nowhere
    -- read exactly like a reason somebody had deliberately excluded, and went
    to nobody. The default is now the loud direction: an unknown reason is
    mailed and logged, so the failure mode of forgetting is one email too many
    rather than an alert that never existed. `SILENT_ALERT_REASONS` is the
    only way to be quiet, and it costs a sentence saying why.
    """
    if kind in _ALWAYS_MAILED_KINDS:
        return True
    if kind != QueueKind.ALERT.value:
        return False
    reason = reason or ""
    if reason in MAILED_ALERT_REASONS:
        return True
    if reason in SILENT_ALERT_REASONS:
        return False
    log.warning(
        "alert reason %r has no entry in MAILED_ALERT_REASONS or "
        "SILENT_ALERT_REASONS; mailing it rather than dropping it. Add it to "
        "domain/services/alert_email.py",
        reason,
    )
    return True


# --------------------------------------------------------------------------
# Rate limiting. Both halves are per-process and in memory on purpose: a
# restart forgetting that it already mailed about something is the harmless
# direction, and a database table read on every alert is not worth it.
# --------------------------------------------------------------------------

_lock = threading.Lock()
#: (reason, subject) -> when it was last mailed.
_last_sent: dict[tuple[str, str], float] = {}
#: The timestamps of every mail in the last hour, for the hard ceiling.
_recent: list[float] = []


def _allowed(snapshot: _Snapshot, *, cooldown: float, max_per_hour: int) -> bool:
    now = time.monotonic()
    key = (snapshot.reason, snapshot.subject_key)
    with _lock:
        _recent[:] = [t for t in _recent if now - t < 3600]
        if max_per_hour > 0 and len(_recent) >= max_per_hour:
            log.warning(
                "alert email NOT sent: %s (%r about %s) -- the %s/hour ceiling "
                "is full (ALERT_EMAIL_MAX_PER_HOUR)",
                snapshot.queue_id,
                snapshot.reason,
                snapshot.subject_key or "-",
                max_per_hour,
            )
            return False
        previous = _last_sent.get(key)
        if previous is not None and now - previous < cooldown:
            log.warning(
                "alert email NOT sent: %s (%r about %s) -- the same reason and "
                "subject was mailed %.0fs ago, inside the %.0fs cooldown "
                "(ALERT_EMAIL_COOLDOWN_SECONDS)",
                snapshot.queue_id,
                snapshot.reason,
                snapshot.subject_key or "-",
                now - previous,
                cooldown,
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


def _why_not() -> str:
    """The transport's own account of itself, for the refusal log line.

    Falls back to saying nothing rather than raising: this runs inside an
    error path, and a log line must never be what breaks one.
    """
    if _describe is None:
        return "no detail available"
    try:
        return _describe()
    except Exception:  # pragma: no cover - a log line must not raise
        return "no detail available"


def _compose(snapshot: _Snapshot) -> tuple[str, str]:
    from config.settings import settings

    if snapshot.kind == QueueKind.HANDOFF.value:
        prefix = "Bot handed over"
    elif snapshot.kind == QueueKind.ITEM_SWAP.value:
        prefix = "Item swap"
    elif snapshot.kind == QueueKind.ITEM_ADD.value:
        # Deliberately not "Item swap" with different words after it. The
        # subject line is what the owner reads on a lock screen, and the one
        # thing they need from it is whether something is coming *off* an
        # order.
        prefix = "Item added to order"
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
        f"Order      : {snapshot.order_id or '-'}",
    ]
    if snapshot.kind == QueueKind.HANDOFF.value:
        lines += [
            "",
            "This conversation is PAUSED -- the bot will not answer this "
            "customer again until a staff member replies to it or resolves "
            "it in the dashboard.",
        ]
    elif snapshot.kind == QueueKind.ITEM_SWAP.value:
        lines += [
            "",
            "A customer is waiting on this: the swap does not happen until "
            "somebody approves or refuses it in the review queue. Approving "
            "it TAKES the original item off the order.",
        ]
    elif snapshot.kind == QueueKind.ITEM_ADD.value:
        lines += [
            "",
            "A customer is waiting on this: the item is not on the order "
            "until somebody approves it in the review queue. Approving it "
            "adds a line and leaves everything already on the order alone.",
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
    """Send one alert, and never fail quietly.

    Every way out of this function that is not a delivered email leaves a log
    line naming the queue item, the reason, what the alert is about, and why
    it stopped -- including the ones that are nobody's fault. An owner who
    asks "why did I not get an email about SWAP-12" must be able to answer it
    from the log rather than from a guess, which is precisely what the
    September 14th item swap could not be.
    """
    mailer = _mailer
    if mailer is None:
        log.warning(
            "alert email NOT sent: %s (%r about %s) -- no mailer is registered "
            "(app.py wires integrations/mail/client.py at boot)",
            snapshot.queue_id,
            snapshot.reason,
            snapshot.subject_key or "-",
        )
        return
    if not _allowed(snapshot, cooldown=cooldown, max_per_hour=max_per_hour):
        return
    subject, body = _compose(snapshot)
    try:
        accepted = mailer(subject, body)
    except Exception:
        # Belt and braces -- the client swallows its own failures too. An
        # unsendable email must never be able to surface anywhere near the
        # code that raised the alert.
        log.exception(
            "alert email NOT sent: %s (%r about %s) -- the mailer raised",
            snapshot.queue_id,
            snapshot.reason,
            snapshot.subject_key or "-",
        )
        return
    if not accepted:
        log.error(
            "alert email NOT sent: %s (%r about %s) -- the mailer refused it: %s",
            snapshot.queue_id,
            snapshot.reason,
            snapshot.subject_key or "-",
            _why_not(),
        )


def notify(session, item) -> None:
    """Queue an email about `item`, to go out once its transaction commits.

    Called by `queues.enqueue` for every queue item; the filtering is here so
    there is one answer to "does this reach the owner", in one file.
    """
    from config.settings import settings

    if _mailer is None:
        log.warning(
            "alert email NOT queued: %s (%r) -- no mailer is registered",
            item.queue_id,
            item.reason,
        )
        return
    if not should_mail(item.kind, item.reason):
        # A deliberate policy skip, not a fault -- `order_confirmed` fires on
        # every sale -- so it is debug rather than warning. It still says
        # which item and why, because "was it filtered or did it fail?" is
        # the first question asked about a missing alert.
        log.debug(
            "alert email not queued for %s: reason %r (kind %s) is not in "
            "MAILED_ALERT_REASONS",
            item.queue_id,
            item.reason,
            item.kind,
        )
        return
    snapshot = _Snapshot(
        queue_id=item.queue_id,
        kind=item.kind,
        reason=item.reason or "",
        summary=item.summary or "",
        channel=item.channel or "",
        external_id=item.external_id or "",
        order_id=item.order_id or "",
        payload=dict(item.payload or {}),
    )
    cooldown = settings.alert_email_cooldown_seconds
    max_per_hour = settings.alert_email_max_per_hour

    def _hook() -> None:
        _spawn(_send, snapshot, cooldown=cooldown, max_per_hour=max_per_hour)

    after_commit(session, _hook)
