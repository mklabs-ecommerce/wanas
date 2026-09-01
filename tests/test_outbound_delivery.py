"""What the customer actually receives, and what the dashboard says they did.

Two production failures live here, and they are the same failure seen from
two sides: the shop believing a message went out.

1. An order produced *two* confirmations on the phone -- the one
   `notifications.order_confirmed` composes and the model's own retelling of
   it -- while the dashboard showed only the model's. The confirmation was
   being written to `sessions` mid-turn and then overwritten by the agent's
   own copy of the history at the end of the turn.

2. A status push more than 24 hours after the customer's last message is
   refused by Meta outright, but was recorded, and shown, exactly like one
   that arrived.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from assistant import runtime as assistant_runtime, session as session_store
from assistant.display import display_history
from assistant.providers.fake import RehearsalProvider
from assistant.runtime import handle_message
from config.settings import settings
from domain.db import SessionLocal, session_scope
from domain.models import Order, QueueKind, utcnow
from domain.services import carts, identities, notifications, orders, queues

CHANNEL = "whatsapp"
WHO = "201000000001"
VARIANT = "wanas-hoodie-s-olive"


def _recording():
    """Wire the transcript port the way `app.py` does at startup.

    Without it none of this is observable at all -- which is why the
    overwrite in (1) never showed up in a test.
    """
    notifications.register_transcript_recorder(assistant_runtime.record_outbound)


def _stop_recording():
    notifications.register_transcript_recorder(None)


def _place(session, *, external_id=WHO, qty=1):
    carts.add(session, CHANNEL, external_id, VARIANT, qty)
    return orders.place_order(
        session,
        channel=CHANNEL,
        external_id=external_id,
        customer_name="Layla",
        governorate="Cairo",
        address="8 Test Street",
        contact_phone="01066667777",
    )


def _seen(session, *, hours_ago: float, external_id=WHO) -> None:
    identity = identities.get_or_create(session, CHANNEL, external_id)
    identity.last_seen_at = utcnow() - timedelta(hours=hours_ago)
    session.flush()


def _texts(session, external_id=WHO):
    return [m.get("content", "") for m in session_store.transcript(session, CHANNEL, external_id)]


# --- one confirmation, and the dashboard sees it --------------------------


def test_an_order_sends_one_confirmation_and_the_transcript_keeps_it(seeded, cairo_rate):
    """The turn that places an order says nothing of its own, and the
    confirmation it did send survives the turn's own history save."""
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    _recording()
    try:
        provider = RehearsalProvider()
        handle_message(CHANNEL, WHO, f"add {VARIANT} 1", db=seeded, provider=provider)
        sender.clear()
        reply = handle_message(
            CHANNEL,
            WHO,
            "order Layla | Cairo | 8 Test Street | 01066667777",
            db=seeded,
            provider=provider,
        )
    finally:
        _stop_recording()
        notifications.register_sender(notifications.LogSender())

    # One message left the shop for this order, and the model did not write it.
    assert reply.silent and not reply.text
    confirmations = [m.text for m in sender.sent if "تم تأكيد طلبك" in m.text]
    assert len(confirmations) == 1, [m.text for m in sender.sent]

    # ...and the dashboard's transcript holds exactly that message. Before the
    # merge in `session.save`, the agent's end-of-turn write overwrote it and
    # staff read a conversation the customer's phone did not agree with.
    stored = _texts(seeded)
    assert stored.count(confirmations[0]) == 1, stored


def test_a_message_written_mid_turn_is_not_overwritten_by_the_turn(seeded):
    """The mechanism on its own: a second writer appends to the row while a
    turn holds an in-memory copy of the history."""
    session_store.save(seeded, CHANNEL, WHO, [{"role": "user", "content": "هاي"}])

    base = session_store.stored_length(seeded, CHANNEL, WHO)
    history = session_store.load(seeded, CHANNEL, WHO)
    history.append({"role": "user", "content": "عايز هودي"})

    # Something else -- an order confirmation -- writes to the row mid-turn.
    assistant_runtime.record_outbound(CHANNEL, WHO, "تم تأكيد طلبك ✅", db=seeded)

    history.append({"role": "assistant", "content": "تمام"})
    session_store.save(seeded, CHANNEL, WHO, history, merge_since=base)

    assert _texts(seeded) == ["هاي", "عايز هودي", "تم تأكيد طلبك ✅", "تمام"]


# --- the 24-hour window ----------------------------------------------------


def _advance(order_id, status):
    with session_scope() as session:
        orders.advance_status(session, session.get(Order, order_id), status)


def test_a_status_push_outside_the_window_is_not_claimed_as_delivered(cairo_rate, monkeypatch):
    """No approved template, no window: nothing is sent, the transcript says
    so, and a person is told to pick up the phone."""
    monkeypatch.setattr(
        notifications,
        "settings",
        dataclasses.replace(settings, whatsapp_template_order_update=""),
    )
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    _recording()
    try:
        with session_scope() as session:
            order_id = _place(session)["order_id"]
            _seen(session, hours_ago=30)
        sender.clear()
        _advance(order_id, "Packed")
    finally:
        _stop_recording()
        notifications.register_sender(notifications.LogSender())

    assert sender.sent == [], "Meta would refuse this; do not pretend otherwise"

    with SessionLocal() as session:
        bubbles = display_history(session_store.transcript(session, CHANNEL, WHO))
        alerts = queues.open_items(session, QueueKind.ALERT.value)

    packed = [b for b in bubbles if "اتجهز" in b["text"]]
    assert len(packed) == 1
    assert packed[0]["delivery"] == "failed"
    assert any(a.reason == "status_push_undelivered" for a in alerts)


def test_a_status_push_outside_the_window_uses_an_approved_template(cairo_rate, monkeypatch):
    """With one configured, the template is what reopens the conversation --
    and the transcript keeps the sentence the customer reads."""
    monkeypatch.setattr(
        notifications,
        "settings",
        dataclasses.replace(settings, whatsapp_template_order_update="wanas_order_update"),
    )
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    _recording()
    try:
        with session_scope() as session:
            order_id = _place(session)["order_id"]
            _seen(session, hours_ago=30)
        sender.clear()
        _advance(order_id, "Packed")
    finally:
        _stop_recording()
        notifications.register_sender(notifications.LogSender())

    assert [m.template for m in sender.sent] == ["wanas_order_update"]

    with SessionLocal() as session:
        bubbles = display_history(session_store.transcript(session, CHANNEL, WHO))
        alerts = queues.open_items(session, QueueKind.ALERT.value)

    packed = [b for b in bubbles if "اتجهز" in b["text"]]
    assert len(packed) == 1 and packed[0]["delivery"] is None
    assert not any(a.reason == "status_push_undelivered" for a in alerts)


def test_the_feedback_request_obeys_the_window_too(cairo_rate, monkeypatch):
    """The rating ask follows a delivery, which is the point furthest from the
    customer's last message -- it is the one most likely to fall outside."""
    monkeypatch.setattr(
        notifications,
        "settings",
        dataclasses.replace(
            settings,
            whatsapp_template_order_update="wanas_order_update",
            whatsapp_template_feedback_request="",
        ),
    )
    sender = notifications.LogSender()
    notifications.register_sender(sender)
    _recording()
    try:
        with session_scope() as session:
            order_id = _place(session)["order_id"]
            _seen(session, hours_ago=30)
        sender.clear()
        for status in ("Packed", "Shipped", "Delivered"):
            _advance(order_id, status)
    finally:
        _stop_recording()
        notifications.register_sender(notifications.LogSender())

    # The three status pushes went as the approved template; the rating ask
    # has none, so it was not sent at all.
    assert [m.template for m in sender.sent] == ["wanas_order_update"] * 3

    with SessionLocal() as session:
        bubbles = display_history(session_store.transcript(session, CHANNEL, WHO))
        alerts = queues.open_items(session, QueueKind.ALERT.value)

    rating = [b for b in bubbles if "تقيّم" in b["text"]]
    assert len(rating) == 1 and rating[0]["delivery"] == "failed"
    assert sum(1 for a in alerts if a.reason == "status_push_undelivered") == 1


def test_a_refused_send_inside_the_window_corrects_the_transcript(cairo_rate):
    """The window was open and the platform refused anyway -- learned only
    after the line was written, so the line is corrected rather than doubled."""

    class _Refusing(notifications.LogSender):
        def send_text(self, to, text, *, template=None):
            message = super().send_text(to, text, template=template)
            message.delivered = False
            message.error = "recipient unreachable"
            return message

    notifications.register_sender(_Refusing())
    _recording()
    try:
        with session_scope() as session:
            order_id = _place(session)["order_id"]
            _seen(session, hours_ago=1)
        _advance(order_id, "Packed")
    finally:
        _stop_recording()
        notifications.register_sender(notifications.LogSender())

    with SessionLocal() as session:
        bubbles = display_history(session_store.transcript(session, CHANNEL, WHO))
        alerts = queues.open_items(session, QueueKind.ALERT.value)

    packed = [b for b in bubbles if "اتجهز" in b["text"]]
    assert len(packed) == 1, "corrected in place, not written a second time"
    assert packed[0]["delivery"] == "failed"
    assert any(a.reason == "status_push_undelivered" for a in alerts)


# --- can the shop tell whether the customer read it? ----------------------


def test_an_order_confirmation_keeps_the_ids_a_read_receipt_needs(seeded, cairo_rate):
    """A confirmation is the message the shop most wants to know landed, and
    it is the one that nearly could not report it.

    An agent reply gets its platform ids from the channel adapter after the
    send (`_remember_sent_ids`). A proactive push has no adapter in the loop:
    it is written inside the order's transaction and sent from an after-commit
    hook, so nothing was stamping the ids on and every confirmation would have
    read as permanently unseen. Without these, `session.record_receipt` has
    nothing to match Meta's callback against.
    """

    class _Numbering(notifications.LogSender):
        """A sender that hands back ids, the way Meta does."""

        def send_text(self, to, text, *, template=None):
            message = super().send_text(to, text, template=template)
            message.message_ids = ["wamid.confirmation.1"]
            return message

    notifications.register_sender(_Numbering())
    _recording()
    try:
        with session_scope() as session:
            _place(session)
    finally:
        _stop_recording()
        notifications.register_sender(notifications.LogSender())

    with SessionLocal() as session:
        stored = session_store.transcript(session, CHANNEL, WHO)
    confirmation = next(m for m in stored if "تم تأكيد طلبك" in m.get("content", ""))
    assert confirmation["mids"] == ["wamid.confirmation.1"]
    # ...and the id is enough to mark it seen when the customer opens it.
    with session_scope() as session:
        assert session_store.record_receipt(
            session, CHANNEL, WHO, "wamid.confirmation.1", "read"
        )
    with SessionLocal() as session:
        seen = next(
            m for m in session_store.transcript(session, CHANNEL, WHO)
            if "تم تأكيد طلبك" in m.get("content", "")
        )
    assert seen["receipt"]["status"] == "read"
