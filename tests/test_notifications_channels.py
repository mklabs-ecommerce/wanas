"""The per-channel outbound sender registry.

`notifications` used to hold one module-level `_sender` for the whole app.
With a second channel that is the wrong-person bug: an Instagram
conversation's staff reply would be posted over WhatsApp, to an IGSID in the
phone field. These tests pin the registry contract -- one client per channel,
and a channel with no registration falls back to the log, never to another
channel's client.
"""

from __future__ import annotations

from domain.db import SessionLocal, session_scope
from domain.models import Channel, QueueKind
from domain.services import (
    notifications,
    queues,
)

INSTAGRAM = Channel.INSTAGRAM_DM.value  # "instagram_dm" -- the plan's constant


def register_whatsapp(sender) -> None:
    notifications.register_sender(sender, channel="whatsapp")


def restore_default() -> None:
    notifications.register_sender(notifications.LogSender(), channel="whatsapp")


# --- the wrong-person test (written first) ---------------------------------


def test_instagram_dm_before_any_registration_never_gets_the_whatsapp_client():
    """A channel with no registered sender must fall back to the log, never
    to another channel's client. Sending an Instagram reply over WhatsApp is
    a message delivered to the wrong person, which is strictly worse than a
    message not sent."""
    whatsapp_client = notifications.LogSender()
    register_whatsapp(whatsapp_client)
    try:
        sender = notifications.get_sender(INSTAGRAM)

        assert isinstance(sender, notifications.LogSender)
        assert sender is not whatsapp_client

        # And sending through it goes nowhere near WhatsApp's outbox.
        sender.send_text("1783475990011", "أهلاً")
        assert whatsapp_client.sent == []
    finally:
        restore_default()


def test_a_registered_channel_is_returned_for_both_none_and_explicit_whatsapp():
    """`get_sender()` with no channel means "the default channel" and is kept
    only for pre-Instagram call sites; it and the explicit form agree."""
    whatsapp_client = notifications.LogSender()
    register_whatsapp(whatsapp_client)
    try:
        assert notifications.get_sender() is whatsapp_client
        assert notifications.get_sender("whatsapp") is whatsapp_client
        # An unregistered channel still does not reach it, from either form.
        assert notifications.get_sender("instagram_dm") is not whatsapp_client
    finally:
        restore_default()


def test_send_proactive_on_an_unregistered_channel_sends_nothing_and_does_not_raise(seeded):
    """Proactive outreach is opt-in per channel: no registered sender means
    nothing is sent and no staff alert is enqueued, not a send through some
    other channel's client."""
    whatsapp_client = notifications.LogSender()
    register_whatsapp(whatsapp_client)
    try:
        with session_scope() as session:
            notifications.send_proactive(
                session,
                INSTAGRAM,
                "1783475990011",
                "لسه متوفر",
                template=None,
                alert_reason="proactive_outreach_failed",
                alert_summary="test",
            )
    finally:
        restore_default()

    assert whatsapp_client.sent == []
    with SessionLocal() as session:
        assert queues.open_items(session, QueueKind.ALERT.value) == []
