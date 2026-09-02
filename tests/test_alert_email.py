"""The owner's alert emails.

Three things had to be true for this to be worth adding, and each is pinned
below: the owner hears about the things that cannot wait, the owner does *not*
hear about the routine ones (an address that carries both is an address that
gets filtered), and nothing about the mail path can reach back into the code
that raised the alert -- not a failure, not a delay, and not a write that
later rolled back.
"""

from __future__ import annotations

import pytest

from domain.models import QueueKind
from domain.services import alert_email, queues


@pytest.fixture()
def mailbox(monkeypatch):
    """A mailer that files the message instead of opening a socket, plus a
    clean rate limiter -- both halves of which are per-process, so without the
    reset the second test in a run would be suppressed by the first."""
    sent: list[tuple[str, str]] = []

    def _mailer(subject: str, body: str) -> bool:
        sent.append((subject, body))
        return True

    monkeypatch.setattr(alert_email, "_mailer", _mailer)
    alert_email.reset_rate_limit()
    yield sent
    alert_email.reset_rate_limit()


def _drain(session) -> None:
    """Commit, which is what fires the queued send (`common/events.py`).

    The tests go through that door rather than calling `_send` directly,
    because "only after the transaction commits" is half of what is being
    pinned here.
    """
    session.commit()


def _mail_inline(monkeypatch):
    """Run the queued send on this thread, so an assertion can follow it.

    Through `alert_email._spawn`, the seam that exists for exactly this --
    rather than replacing `threading.Thread` on the standard library module,
    which every other thread in the suite is also using.
    """
    monkeypatch.setattr(
        alert_email, "_spawn", lambda fn, snapshot, **kwargs: fn(snapshot, **kwargs)
    )


# --------------------------------------------------------------------------
# What reaches the owner, and what does not.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,reason",
    [
        (QueueKind.HANDOFF.value, "complaint"),
        (QueueKind.HANDOFF.value, "customer_asked"),
        (QueueKind.ALERT.value, "negative_comment"),
        (QueueKind.ALERT.value, "customer_complaint"),
        (QueueKind.ALERT.value, "turn_crashed"),
        (QueueKind.ALERT.value, "reply_delivery_failed"),
        (QueueKind.ALERT.value, "instagram_token_refresh_failed"),
    ],
)
def test_the_three_things_the_owner_asked_about_are_mailed(kind, reason):
    assert alert_email.should_mail(kind, reason) is True


@pytest.mark.parametrize(
    "kind,reason",
    [
        # Routine, and frequent. An inbox carrying these is an inbox nobody
        # reads, which costs exactly the alerts above.
        (QueueKind.ALERT.value, "order_confirmed"),
        (QueueKind.ALERT.value, "low_stock"),
        (QueueKind.ALERT.value, "order_modified"),
        (QueueKind.ALERT.value, "order_cancelled"),
        # A swap is a decision for staff to make, not an emergency.
        (QueueKind.ITEM_SWAP.value, "swap_requested"),
    ],
)
def test_the_routine_queue_items_stay_in_the_dashboard(kind, reason):
    assert alert_email.should_mail(kind, reason) is False


def test_a_handoff_is_mailed_whatever_its_reason():
    """A handoff *pauses* the conversation, so the customer is unanswered
    until a person opens the dashboard. That is true of every reason, which is
    why handoffs are not filtered by one."""
    assert alert_email.should_mail(QueueKind.HANDOFF.value, "a_reason_nobody_has_written_yet")


# --------------------------------------------------------------------------
# The path from `enqueue` to the mailbox.
# --------------------------------------------------------------------------


def test_enqueueing_a_handoff_emails_the_owner(db, mailbox, monkeypatch):
    _mail_inline(monkeypatch)
    queues.enqueue(
        db,
        kind=QueueKind.HANDOFF.value,
        reason="complaint",
        summary="Customer says the hoodie arrived torn",
        channel="whatsapp",
        external_id="201234567890",
    )
    assert mailbox == []  # nothing before the commit
    _drain(db)

    (subject, body), = mailbox
    assert "torn" in subject
    assert "whatsapp" in subject
    assert "201234567890" in body
    assert "PAUSED" in body


def test_a_negative_instagram_comment_emails_the_owner(db, mailbox, monkeypatch):
    _mail_inline(monkeypatch)
    queues.enqueue(
        db,
        kind=QueueKind.ALERT.value,
        reason="negative_comment",
        summary="Negative comment from someone on media 17",
        channel="instagram_dm",
        external_id="ig-user-1",
        payload={"comment_id": "c1", "text": "worst shop ever"},
    )
    _drain(db)

    (subject, body), = mailbox
    assert subject.startswith("[Wanas] Negative comment")
    assert "worst shop ever" in body
    assert "c1" in body


def test_a_routine_alert_sends_no_email(db, mailbox, monkeypatch):
    _mail_inline(monkeypatch)
    queues.enqueue(
        db,
        kind=QueueKind.ALERT.value,
        reason="order_confirmed",
        summary="Order W-1 confirmed",
        channel="whatsapp",
        external_id="201234567890",
    )
    _drain(db)
    assert mailbox == []


def test_a_rolled_back_alert_is_never_mailed(db, mailbox, monkeypatch):
    """The whole reason the send hangs off the commit.

    An email about a handoff whose transaction died is a person opening the
    dashboard to look for a queue item that does not exist.
    """
    _mail_inline(monkeypatch)
    queues.enqueue(
        db,
        kind=QueueKind.HANDOFF.value,
        reason="unclear",
        summary="Should never be mailed",
        channel="whatsapp",
        external_id="201234567890",
    )
    db.rollback()
    assert mailbox == []


def test_the_queue_item_is_written_even_when_the_mailer_raises(db, monkeypatch):
    """The alert is the record; the email is a courtesy on top of it. A mail
    server having a bad day must not be able to lose a handoff."""
    _mail_inline(monkeypatch)

    def _explode(subject: str, body: str) -> bool:
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(alert_email, "_mailer", _explode)
    alert_email.reset_rate_limit()

    item = queues.enqueue(
        db,
        kind=QueueKind.HANDOFF.value,
        reason="complaint",
        summary="Still has to be queued",
        channel="whatsapp",
        external_id="201234567890",
    )
    _drain(db)
    assert queues.open_items(db, QueueKind.HANDOFF.value)[0].queue_id == item.queue_id


# --------------------------------------------------------------------------
# Rate limiting: one queue item per event is right, one email per event is not.
# --------------------------------------------------------------------------


def test_the_same_reason_about_the_same_customer_is_mailed_once(db, mailbox, monkeypatch):
    _mail_inline(monkeypatch)
    for i in range(5):
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason="turn_crashed",
            summary=f"crash {i}",
            channel="whatsapp",
            external_id="201234567890",
        )
        _drain(db)
    assert len(mailbox) == 1


def test_a_different_customer_is_a_different_email(db, mailbox, monkeypatch):
    """The cooldown must not silence a second person's problem."""
    _mail_inline(monkeypatch)
    for who in ("201111111111", "202222222222"):
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason="turn_crashed",
            summary="crash",
            channel="whatsapp",
            external_id=who,
        )
        _drain(db)
    assert len(mailbox) == 2


def test_the_hourly_ceiling_holds(mailbox):
    """A bug that raises an alert a second must not turn the owner's inbox
    into the log file.

    Driven through `_send` directly: the limits reach it as arguments, and
    `settings` is a frozen dataclass with no seam to lower them through.
    """
    for i in range(10):
        alert_email._send(
            alert_email._Snapshot(
                queue_id=f"A-{i}",
                kind=QueueKind.ALERT.value,
                reason="turn_crashed",
                summary="crash",
                channel="whatsapp",
                external_id=f"2011111111{i:02d}",
                payload={},
            ),
            cooldown=0.0,
            max_per_hour=3,
        )
    assert len(mailbox) == 3


# --------------------------------------------------------------------------
# The transport itself.
# --------------------------------------------------------------------------


def test_the_client_is_inert_without_credentials():
    """Same shape as every other integration here: not configured is a
    documented off state, not an error."""
    from integrations.mail.client import send_email

    assert send_email("subject", "body") is False
