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
        # An order or the shelf changing after the fact. Not the bot going
        # wrong -- ordinary business events, but every one of them is stock
        # or money moving, which is why the owner asked for them.
        (QueueKind.ALERT.value, "order_modified"),
        (QueueKind.ALERT.value, "order_cancelled"),
        (QueueKind.ALERT.value, "low_stock"),
        (QueueKind.ITEM_SWAP.value, "swap_requested"),
    ],
)
def test_what_the_owner_asked_to_be_interrupted_for_is_mailed(kind, reason):
    assert alert_email.should_mail(kind, reason) is True


def test_a_confirmed_order_stays_in_the_dashboard():
    """The one deliberately silent reason, and it is the loud one.

    `order_confirmed` fires on every successful sale -- the outcome the whole
    system exists to produce. An address carrying it is an address that gets
    filtered, and filtering it costs every alert above.
    """
    assert alert_email.should_mail(QueueKind.ALERT.value, "order_confirmed") is False


def test_an_unknown_alert_reason_is_not_mailed():
    """The list is an allow-list, so a reason added elsewhere later does not
    start mailing the owner by accident -- it has to be put here on purpose."""
    assert alert_email.should_mail(QueueKind.ALERT.value, "some_new_reason") is False
    assert alert_email.should_mail(QueueKind.ALERT.value, None) is False


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


def test_a_confirmed_order_sends_no_email(db, mailbox, monkeypatch):
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


def test_an_item_swap_emails_the_owner(db, mailbox, monkeypatch):
    """A swap is a customer waiting on a decision only a person can make --
    the order does not move until somebody makes it."""
    _mail_inline(monkeypatch)
    queues.enqueue(
        db,
        kind=QueueKind.ITEM_SWAP.value,
        reason="swap_requested",
        summary="W-7: swap the black hoodie for the grey one",
        channel="whatsapp",
        external_id="201234567890",
    )
    _drain(db)

    (subject, body), = mailbox
    assert subject.startswith("[Wanas] Item swap")
    assert "does not happen until" in body


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


def test_two_different_products_going_low_are_two_emails(db, mailbox, monkeypatch):
    """The cooldown keys on *what the alert is about*, not on `external_id`.

    `low_stock`, `order_modified` and `order_cancelled` carry no external_id
    at all -- they are raised about a variant or an order, not about whoever
    happened to be typing. Keying on external_id alone collapsed every one of
    them onto a single empty key, so the second product to run low inside the
    window was never mailed.
    """
    _mail_inline(monkeypatch)
    for variant_id in ("V-1", "V-2"):
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason="low_stock",
            summary=f"Low stock: {variant_id}",
            payload={"variant_id": variant_id, "stock_qty": 1},
        )
        _drain(db)
    assert len(mailbox) == 2


def test_the_same_product_going_low_twice_is_one_email(db, mailbox, monkeypatch):
    _mail_inline(monkeypatch)
    for _ in range(4):
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason="low_stock",
            summary="Low stock: V-1",
            payload={"variant_id": "V-1", "stock_qty": 1},
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
                order_id="",
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


# --------------------------------------------------------------------------
# Choosing a transport
# --------------------------------------------------------------------------
#
# Railway blocks every outbound SMTP port -- 25, 465, 587 and 2525 all answer
# "Network is unreachable" from inside the container while plain HTTP
# connects instantly. So the app password that works from a laptop cannot
# deliver from production, and the Gmail API over 443 is what does.


def test_the_gmail_api_wins_when_it_is_configured(monkeypatch):
    """It is the only transport that can leave a Railway container, so it is
    not merely preferred -- SMTP there is a send that silently never lands."""
    import dataclasses

    from config.settings import settings as real
    from integrations.mail import client as mail_client

    calls: list[str] = []
    monkeypatch.setattr(
        mail_client,
        "settings",
        dataclasses.replace(
            real,
            alert_email_to="owner@example.com",
            gmail_client_id="id",
            gmail_client_secret="secret",
            gmail_refresh_token="refresh",
            alert_smtp_host="smtp.example.com",
            alert_smtp_username="user",
            alert_smtp_password="pass",
        ),
    )
    monkeypatch.setattr(
        mail_client.gmail_api, "send_email", lambda s, b: calls.append("gmail") or True
    )
    monkeypatch.setattr(
        mail_client, "send_over_smtp", lambda s, b: calls.append("smtp") or True
    )

    assert mail_client.send_email("subject", "body") is True
    assert calls == ["gmail"]


def test_smtp_still_answers_where_there_is_no_gmail_grant(monkeypatch):
    """SMTP is not dead code: it is what a developer's machine has, and what
    keeps this portable off Railway."""
    import dataclasses

    from config.settings import settings as real
    from integrations.mail import client as mail_client

    calls: list[str] = []
    monkeypatch.setattr(
        mail_client,
        "settings",
        dataclasses.replace(
            real,
            alert_email_to="owner@example.com",
            gmail_client_id="",
            gmail_client_secret="",
            gmail_refresh_token="",
            alert_smtp_host="smtp.example.com",
            alert_smtp_username="user",
            alert_smtp_password="pass",
        ),
    )
    monkeypatch.setattr(mail_client, "send_over_smtp", lambda s, b: calls.append("smtp") or True)

    assert mail_client.send_email("subject", "body") is True
    assert calls == ["smtp"]


def test_resend_wins_over_every_other_transport(monkeypatch):
    """Both HTTPS routes leave a Railway container, so this is not about
    reachability -- it is about what expires. The Gmail grant dies after seven
    days in Testing, on a password change, or on a revoked consent, and the
    channel that would warn about it is the channel that broke. A long-lived
    key has none of that, so it goes first."""
    import dataclasses

    from config.settings import settings as real
    from integrations.mail import client as mail_client

    calls: list[str] = []
    monkeypatch.setattr(
        mail_client,
        "settings",
        dataclasses.replace(
            real,
            alert_email_to="owner@example.com",
            resend_api_key="key",
            gmail_client_id="id",
            gmail_client_secret="secret",
            gmail_refresh_token="refresh",
            alert_smtp_host="smtp.example.com",
            alert_smtp_username="user",
            alert_smtp_password="pass",
        ),
    )
    monkeypatch.setattr(
        mail_client.resend, "send_email", lambda s, b: calls.append("resend") or True
    )
    monkeypatch.setattr(
        mail_client.gmail_api, "send_email", lambda s, b: calls.append("gmail") or True
    )
    monkeypatch.setattr(mail_client, "send_over_smtp", lambda s, b: calls.append("smtp") or True)

    assert mail_client.send_email("subject", "body") is True
    assert calls == ["resend"]


def test_the_resend_client_is_inert_without_a_key():
    """Same rule as every other transport: not configured is an off state, not
    an error -- the alert is already in the staff queue either way."""
    from integrations.mail import resend

    assert resend.send_email("subject", "body") is False


def test_resend_posts_the_alert_as_plain_text(monkeypatch):
    """The one shape assertion: a bearer key in the header, the owner in `to`,
    and the body as `text` -- an alert is a plain message, not an HTML mail."""
    import dataclasses

    import httpx

    from config.settings import settings as real
    from integrations.mail import resend

    monkeypatch.setattr(
        resend,
        "settings",
        dataclasses.replace(
            real,
            alert_email_to="owner@example.com",
            resend_api_key="key",
            resend_from="alerts@wanas.example",
        ),
    )
    seen: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json)
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(resend.httpx, "post", fake_post)

    assert resend.send_email("subject", "body") is True
    assert seen["url"] == resend.SEND_URL
    assert seen["headers"]["Authorization"] == "Bearer key"
    assert seen["json"] == {
        "from": "alerts@wanas.example",
        "to": ["owner@example.com"],
        "subject": "subject",
        "text": "body",
    }


def test_a_refused_resend_send_is_false_not_an_exception(monkeypatch):
    """An unverified sender domain is the refusal that actually happens, and
    it must not break the path that raised the alert."""
    import dataclasses

    import httpx

    from config.settings import settings as real
    from integrations.mail import resend

    monkeypatch.setattr(
        resend,
        "settings",
        dataclasses.replace(real, alert_email_to="owner@example.com", resend_api_key="key"),
    )
    monkeypatch.setattr(
        resend.httpx,
        "post",
        lambda *a, **k: httpx.Response(403, json={"message": "domain is not verified"}),
    )

    assert resend.send_email("subject", "body") is False


def test_a_resend_key_alone_is_enough_to_have_email_configured():
    """`resend_from` has a working default, so unlike the Gmail grant there is
    no partial state to guard against."""
    import dataclasses

    from config.settings import settings as real

    configured = dataclasses.replace(
        real,
        alert_email_to="owner@example.com",
        resend_api_key="key",
        gmail_client_id="",
        gmail_client_secret="",
        gmail_refresh_token="",
        alert_smtp_username="",
        alert_smtp_password="",
    )
    assert configured.resend_configured is True
    assert configured.alert_email_configured is True
    assert dataclasses.replace(configured, alert_email_to="").alert_email_configured is False


def test_a_partial_gmail_grant_is_not_configured():
    """Two of the three values is not a usable credential, and treating it as
    one would pick a transport that cannot authenticate over the one that
    can."""
    import dataclasses

    from config.settings import settings as real

    partial = dataclasses.replace(
        real, gmail_client_id="id", gmail_client_secret="secret", gmail_refresh_token=""
    )
    assert partial.gmail_api_configured is False


def test_either_transport_counts_as_configured():
    import dataclasses

    from config.settings import settings as real

    gmail_only = dataclasses.replace(
        real,
        alert_email_to="owner@example.com",
        gmail_client_id="id",
        gmail_client_secret="secret",
        gmail_refresh_token="refresh",
        alert_smtp_host="",
        alert_smtp_username="",
        alert_smtp_password="",
    )
    assert gmail_only.alert_email_configured is True

    # ...and no recipient is off, whatever the transport says.
    assert dataclasses.replace(gmail_only, alert_email_to="").alert_email_configured is False


def test_the_gmail_client_is_inert_without_a_grant():
    from integrations.mail import gmail_api

    assert gmail_api.send_email("subject", "body") is False


def test_an_expired_refresh_token_is_reported_not_raised(monkeypatch):
    """The failure this route is known for. It must be loud in the log and
    invisible to the code that raised the alert -- there is no second channel
    to warn through, because the thing that broke is the warning channel.
    """
    import dataclasses

    import httpx

    from config.settings import settings as real
    from integrations.mail import gmail_api

    monkeypatch.setattr(
        gmail_api,
        "settings",
        dataclasses.replace(
            real,
            alert_email_to="owner@example.com",
            gmail_client_id="id",
            gmail_client_secret="secret",
            gmail_refresh_token="stale",
        ),
    )
    gmail_api.reset_token_cache()

    class _Refused:
        status_code = 400

        def json(self):
            return {"error": "invalid_grant"}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Refused())
    assert gmail_api.send_email("subject", "body") is False
    gmail_api.reset_token_cache()
