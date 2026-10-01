"""Back-in-stock waitlisting (the idle-cart nudge moved to test_silence_nudges.py).

Feature 1 (status pushes) and Feature 2 (feedback request) already have
coverage in test_backend_services.py::test_status_pushes_and_feedback_request
-- both were fully wired before this file existed. This covers what was
actually new: `domain/services/waitlist.py`, `domain/services/
reengagement.py`, and `notifications.send_proactive`'s 24-hour window logic.
"""

from __future__ import annotations

from datetime import timedelta

from assistant import runtime as assistant_runtime, session as session_store
from domain.db import SessionLocal, session_scope
from domain.models import (
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

VARIANT = "rehla-hoodie-s-olive"
SOLD_OUT = "rehla-hoodie-m-olive"
CHANNEL = "whatsapp"
CUSTOMER = "201000000001"


def _seen(session, *, hours_ago: float) -> None:
    identity = identities.get_or_create(session, CHANNEL, CUSTOMER)
    identity.last_seen_at = utcnow() - timedelta(hours=hours_ago)
    session.flush()


# --- waitlist.join ----------------------------------------------------------


def test_join_is_idempotent(seeded):
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
    seeded.commit()
    assert len(waitlist.open_entries(seeded)) == 1


def test_join_rearms_after_notification(seeded):
    entry = waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
    entry.notified_at = utcnow()
    seeded.flush()
    assert waitlist.open_entries(seeded) == []

    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
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
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
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
    assert "REHLA Hoodie" in sender.sent[0].text
    with SessionLocal() as session:
        entries = session.query(StockWaitlistEntry).all()
        assert entries[0].notified_at is not None


def test_still_out_of_stock_leaves_the_entry_open(seeded, shopify):
    _seen(seeded, hours_ago=1)
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
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


# --- "back in stock" must describe an actual change -------------------------


def test_no_restock_is_announced_for_an_item_that_never_left_the_shelf(seeded, shopify):
    """The bug this guard exists for, end to end at the service level.

    An entry whose baseline says the item was on the shelf when the customer
    was turned away is not evidence of a restock, however healthy the count
    looks now -- so nothing is sent and the entry stays open.
    """
    _seen(seeded, hours_ago=1)
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=2)
    seeded.commit()

    shopify.set(SOLD_OUT, qty=5)

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        notified = reengagement.check_back_in_stock()
    finally:
        notifications.register_sender(notifications.LogSender())

    assert notified == 0
    assert sender.sent == []
    with SessionLocal() as session:
        assert session.query(StockWaitlistEntry).one().notified_at is None


def test_an_entry_with_no_baseline_is_baselined_rather_than_announced(seeded, shopify):
    """Rows written before `observed_stock` existed cannot prove a transition,
    so the first pass records one instead of guessing at one."""
    _seen(seeded, hours_ago=1)
    entry = waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
    entry.observed_stock = None
    seeded.commit()

    shopify.set(SOLD_OUT, qty=5)

    sender = notifications.LogSender()
    notifications.register_sender(sender)
    try:
        notified = reengagement.check_back_in_stock()
    finally:
        notifications.register_sender(notifications.LogSender())

    assert notified == 0
    assert sender.sent == []
    with SessionLocal() as session:
        fresh = session.query(StockWaitlistEntry).one()
        assert fresh.observed_stock == 5
        assert fresh.notified_at is None


# --- every message the shop starts reaches the transcript -------------------


def test_a_proactive_message_is_written_into_the_transcript(seeded, shopify):
    """The dashboard reads `sessions`. A back-in-stock notice that reaches the
    customer's phone and not that table is a transcript staff cannot trust."""
    _seen(seeded, hours_ago=1)
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
    seeded.commit()
    shopify.set(SOLD_OUT, qty=5)

    notifications.register_sender(notifications.LogSender())
    notifications.register_transcript_recorder(assistant_runtime.record_outbound)
    try:
        assert reengagement.check_back_in_stock() == 1
    finally:
        notifications.register_transcript_recorder(None)
        notifications.register_sender(notifications.LogSender())

    with SessionLocal() as session:
        history = session_store.transcript(session, CHANNEL, CUSTOMER)
    assert len(history) == 1
    assert history[0]["role"] == "assistant"
    # Neither the model's words nor a staff member's.
    assert history[0]["by"] == "system"
    assert "REHLA Hoodie" in history[0]["content"]


def test_a_message_that_did_not_land_is_written_down_as_undelivered(seeded, shopify):
    """An undelivered message is not something the customer was told.

    It is still kept -- the staff member picking up the alert needs to read
    what the shop meant to say -- but flagged, so nothing downstream can show
    it as a message the customer received."""
    _seen(seeded, hours_ago=1)
    waitlist.join(seeded, SOLD_OUT, CHANNEL, CUSTOMER, observed_stock=0)
    seeded.commit()
    shopify.set(SOLD_OUT, qty=5)

    class _Refusing(notifications.LogSender):
        def send_text(self, to, text, *, template=None):
            message = super().send_text(to, text, template=template)
            message.delivered = False
            message.error = "outside the 24h window"
            return message

    notifications.register_sender(_Refusing())
    notifications.register_transcript_recorder(assistant_runtime.record_outbound)
    try:
        reengagement.check_back_in_stock()
    finally:
        notifications.register_transcript_recorder(None)
        notifications.register_sender(notifications.LogSender())

    with SessionLocal() as session:
        history = session_store.transcript(session, CHANNEL, CUSTOMER)
    assert [m["content"] for m in history] == [
        notifications.BACK_IN_STOCK_TEXT.format(product_name="REHLA Hoodie")
    ]
    assert history[0]["delivery"] == "failed"
