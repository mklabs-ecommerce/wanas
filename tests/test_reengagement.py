"""Back-in-stock waitlisting and abandoned-cart follow-up.

Feature 1 (status pushes) and Feature 2 (feedback request) already have
coverage in test_backend_services.py::test_status_pushes_and_feedback_request
-- both were fully wired before this file existed. This covers what was
actually new: `domain/services/waitlist.py`, `domain/services/
reengagement.py`, and `notifications.send_proactive`'s 24-hour window logic.
"""

from __future__ import annotations

from datetime import timedelta

from domain.db import SessionLocal, session_scope
from domain.models import (
    AbandonedCartNudge,
    CartItem,
    QueueKind,
    StockWaitlistEntry,
    utcnow,
)
from domain.services import (
    identities,
    notifications,
    queues,
    reengagement,
    waitlist,
)

VARIANT = "wanas-hoodie-s-olive"
SOLD_OUT = "wanas-hoodie-m-olive"
CHANNEL = "whatsapp"
CUSTOMER = "201000000001"


def _seen(session, *, hours_ago: float) -> None:
    identity = identities.get_or_create(session, CHANNEL, CUSTOMER)
    identity.last_seen_at = utcnow() - timedelta(hours=hours_ago)
    session.flush()


# --- waitlist.join ----------------------------------------------------------


def test_join_is_idempotent(seeded):
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER)
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER)
    seeded.commit()
    assert len(waitlist.open_entries(seeded)) == 1


def test_join_rearms_after_notification(seeded):
    entry = waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER)
    entry.notified_at = utcnow()
    seeded.flush()
    assert waitlist.open_entries(seeded) == []

    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER)
    seeded.commit()
    entries = waitlist.open_entries(seeded)
    assert len(entries) == 1
    assert entries[0].notified_at is None


# --- notifications.send_proactive -------------------------------------------


def test_send_proactive_within_window_sends_free_text(seeded):
    _seen(seeded, hours_ago=1)
    seeded.commit()

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        with session_scope() as session:
            notifications.send_proactive(
                session, CHANNEL, CUSTOMER, "hello",
                template="approved_template", alert_reason="proactive_outreach_failed",
                alert_summary="test",
            )
    finally:
        notifications.register_sender(notifications.LogSender())

    assert len(sender.sent) == 1
    assert sender.sent[0].text == "hello"
    with SessionLocal() as session:
        assert queues.open_items(session, QueueKind.ALERT.value) == []


def test_send_proactive_outside_window_uses_template(seeded):
    _seen(seeded, hours_ago=30)
    seeded.commit()

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        with session_scope() as session:
            notifications.send_proactive(
                session, CHANNEL, CUSTOMER, "hello",
                template="approved_template", alert_reason="proactive_outreach_failed",
                alert_summary="test",
            )
    finally:
        notifications.register_sender(notifications.LogSender())

    assert len(sender.sent) == 1
    assert sender.sent[0].template == "approved_template"
    with SessionLocal() as session:
        assert queues.open_items(session, QueueKind.ALERT.value) == []


def test_send_proactive_outside_window_with_no_template_alerts_staff(seeded):
    _seen(seeded, hours_ago=30)
    seeded.commit()

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        with session_scope() as session:
            notifications.send_proactive(
                session, CHANNEL, CUSTOMER, "hello",
                template=None, alert_reason="proactive_outreach_failed",
                alert_summary="needs a person",
            )
    finally:
        notifications.register_sender(notifications.LogSender())

    # Nothing was sent -- no window, no approved template to reopen it.
    assert sender.sent == []
    with SessionLocal() as session:
        alerts = queues.open_items(session, QueueKind.ALERT.value)
        assert any(a.reason == "proactive_outreach_failed" for a in alerts)


def test_send_proactive_never_seen_treats_window_as_closed(seeded):
    # No ChannelIdentity row at all -- this customer has never messaged in,
    # which must not be read as "the window happens to be open".
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        with session_scope() as session:
            notifications.send_proactive(
                session, CHANNEL, "201099999999", "hello",
                template=None, alert_reason="proactive_outreach_failed",
                alert_summary="needs a person",
            )
    finally:
        notifications.register_sender(notifications.LogSender())

    assert sender.sent == []
    with SessionLocal() as session:
        assert any(
            a.reason == "proactive_outreach_failed"
            for a in queues.open_items(session, QueueKind.ALERT.value)
        )


def test_send_proactive_ignores_non_whatsapp_channels(seeded):
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        with session_scope() as session:
            notifications.send_proactive(
                session, "instagram_dm", "someone", "hello",
                template=None, alert_reason="proactive_outreach_failed",
                alert_summary="test",
            )
    finally:
        notifications.register_sender(notifications.LogSender())

    assert sender.sent == []
    with SessionLocal() as session:
        assert queues.open_items(session, QueueKind.ALERT.value) == []


# --- reengagement.check_back_in_stock ---------------------------------------


def test_back_in_stock_notifies_and_closes_the_entry(seeded, shopify):
    _seen(seeded, hours_ago=1)
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER)
    seeded.commit()

    shopify.set(SOLD_OUT, qty=5)

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        notified = reengagement.check_back_in_stock()
    finally:
        notifications.register_sender(notifications.LogSender())

    assert notified == 1
    assert len(sender.sent) == 1
    assert "WANAS Hoodie" in sender.sent[0].text
    with SessionLocal() as session:
        entries = session.query(StockWaitlistEntry).all()
        assert entries[0].notified_at is not None


def test_still_out_of_stock_leaves_the_entry_open(seeded, shopify):
    _seen(seeded, hours_ago=1)
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER)
    seeded.commit()

    shopify.set(SOLD_OUT, qty=0)

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        notified = reengagement.check_back_in_stock()
    finally:
        notifications.register_sender(notifications.LogSender())

    assert notified == 0
    assert sender.sent == []
    with SessionLocal() as session:
        assert len(waitlist.open_entries(session)) == 1


# --- reengagement.check_abandoned_carts -------------------------------------


def _add_line(session, *, hours_ago: float) -> None:
    session.add(
        CartItem(
            channel=CHANNEL,
            external_id=CUSTOMER,
            variant_id=VARIANT,
            quantity=1,
            added_at=utcnow() - timedelta(hours=hours_ago),
        )
    )
    session.commit()


def test_idle_cart_gets_nudged_once(seeded):
    _seen(seeded, hours_ago=7)
    _add_line(seeded, hours_ago=7)

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        nudged = reengagement.check_abandoned_carts()
        assert nudged == 1
        assert len(sender.sent) == 1

        # Running it again immediately must not nudge a second time.
        sender.clear()
        nudged_again = reengagement.check_abandoned_carts()
    finally:
        notifications.register_sender(notifications.LogSender())

    assert nudged_again == 0
    assert sender.sent == []
    with SessionLocal() as session:
        assert session.get(AbandonedCartNudge, (CHANNEL, CUSTOMER)) is not None


def test_fresh_cart_is_not_nudged(seeded):
    _seen(seeded, hours_ago=1)
    _add_line(seeded, hours_ago=1)

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        nudged = reengagement.check_abandoned_carts()
    finally:
        notifications.register_sender(notifications.LogSender())

    assert nudged == 0
    assert sender.sent == []


def test_ancient_cart_is_not_nudged(seeded):
    """Past ABANDONED_CART_MAX_AGE_HOURS the cart is dead, not abandoned --
    an old test cart must not get nudged on every restart forever."""
    _seen(seeded, hours_ago=50)
    _add_line(seeded, hours_ago=50)

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        nudged = reengagement.check_abandoned_carts()
    finally:
        notifications.register_sender(notifications.LogSender())

    assert nudged == 0
    assert sender.sent == []
