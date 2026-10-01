"""The "customer went silent" nudges -- assistant/silence_nudges.py."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from assistant import messages as msg, runtime as assistant_runtime, silence_nudges
from assistant.providers import set_provider
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from config.settings import settings
from domain.db import SessionLocal, session_scope
from domain.models import CartItem, Channel, SessionRow, SilenceNudge, utcnow
from domain.services import identities, notifications

CUSTOMER = "201000000077"
IGSID = "17841400000000077"
VARIANT = "rehla-hoodie-s-olive"
WRITTEN = "لسه معانا؟ 🙂 لو حابين نكمل في الهودي أنا موجود."


@pytest.fixture()
def sender():
    wa, ig = notifications.LogSender(), notifications.LogSender()
    notifications.register_sender(wa, channel="whatsapp")
    notifications.register_sender(ig, channel=Channel.INSTAGRAM_DM.value)
    notifications.register_transcript_recorder(assistant_runtime.record_outbound)
    yield wa, ig
    notifications.register_transcript_recorder(None)
    notifications.register_sender(notifications.LogSender(), channel="whatsapp")
    notifications._senders.pop(Channel.INSTAGRAM_DM.value, None)


@pytest.fixture(autouse=True)
def daytime(monkeypatch):
    """No quiet hours unless a test asks for them; the model writes WRITTEN."""
    patched = dataclasses.replace(settings, nudge_quiet_start_hour=0, nudge_quiet_end_hour=0)
    monkeypatch.setattr(silence_nudges, "settings", patched)
    set_provider(ScriptedProvider([ModelReply(text=WRITTEN)] * 5))
    yield
    set_provider(None)


def quiet_now(monkeypatch):
    """Make whatever hour it is in Cairo right now a quiet hour."""
    hour = utcnow().astimezone(silence_nudges.CAIRO).hour
    patched = dataclasses.replace(
        settings, nudge_quiet_start_hour=hour, nudge_quiet_end_hour=(hour + 1) % 24
    )
    monkeypatch.setattr(silence_nudges, "settings", patched)


def conversation(
    *,
    minutes_ago: float,
    customer: str = "عايزة الهودي الزيتي",
    reply: str = "متوفر 👌 تحب مقاس إيه؟",
    by: str | None = None,
    channel: str = "whatsapp",
    external_id: str = CUSTOMER,
    cart: bool = False,
) -> datetime:
    at = utcnow() - timedelta(minutes=minutes_ago)
    with session_scope() as db:
        identity = identities.get_or_create(db, channel, external_id)
        identity.last_seen_at = at
        history = [msg.user(customer, at=at.isoformat())]
        if reply is not None:
            history.append(msg.assistant(reply, by=by))
        db.add(SessionRow(channel=channel, external_id=external_id, history=history, updated_at=at))
        if cart:
            db.add(CartItem(channel=channel, external_id=external_id, variant_id=VARIANT, quantity=1))
    return at


def customer_writes(minutes_ago: float = 0, external_id: str = CUSTOMER) -> None:
    with session_scope() as db:
        identity = identities.get(db, "whatsapp", external_id)
        identity.last_seen_at = utcnow() - timedelta(minutes=minutes_ago)
        row = db.get(SessionRow, ("whatsapp", external_id))
        row.history = [*row.history, msg.user("لسه بفكر"), msg.assistant("تمام، براحتك 🙂")]


def row(external_id: str = CUSTOMER, channel: str = "whatsapp") -> SilenceNudge | None:
    with SessionLocal() as db:
        return db.get(SilenceNudge, (channel, external_id))


def later(hours: float) -> datetime:
    return utcnow() + timedelta(hours=hours)


# --- nudge #1 ---------------------------------------------------------------


def test_nudge_one_fires_ten_minutes_after_the_bot_replied(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11)

    assert silence_nudges.check_silences() == 1
    assert [m.text for m in wa.sent] == [WRITTEN]
    assert row().first_state == "sent"
    with SessionLocal() as db:
        history = db.get(SessionRow, ("whatsapp", CUSTOMER)).history
    assert history[-1]["content"] == WRITTEN and history[-1]["by"] == "system"


def test_nudge_one_waits_for_the_ten_minutes(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=5)
    assert silence_nudges.check_silences() == 0
    assert wa.sent == []


def test_nudge_one_is_not_sent_while_her_message_is_unanswered(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=30, reply=None)
    assert silence_nudges.check_silences() == 0
    assert wa.sent == [] and row().first_state is None


def test_nudge_one_works_on_instagram(seeded, sender):
    _, ig = sender
    conversation(minutes_ago=11, channel=Channel.INSTAGRAM_DM.value, external_id=IGSID)
    assert silence_nudges.check_silences() == 1
    assert [m.to for m in ig.sent] == [IGSID]


def test_a_doubtful_model_line_falls_back_to_the_fixed_one(seeded, sender):
    wa, _ = sender
    set_provider(ScriptedProvider([ModelReply(text="الهودي بـ 850 جنيه بس النهارده!!")]))
    conversation(minutes_ago=11)
    assert silence_nudges.check_silences() == 1
    assert wa.sent[0].text == silence_nudges.FIRST_FALLBACK


def test_a_second_pass_never_sends_nudge_one_again(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11)
    assert silence_nudges.check_silences() == 1
    assert silence_nudges.check_silences() == 0
    assert len(wa.sent) == 1


# --- nudge #2 ---------------------------------------------------------------


def test_nudge_two_needs_an_open_cart(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11)
    silence_nudges.check_silences()
    assert silence_nudges.check_silences(now=later(2)) == 0
    assert len(wa.sent) == 1
    assert row().second_state == "skipped:no_cart"


def test_nudge_two_goes_at_two_hours_with_a_cart(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11, cart=True)
    silence_nudges.check_silences()
    assert silence_nudges.check_silences(now=later(1)) == 0  # not yet
    assert silence_nudges.check_silences(now=later(2)) == 1
    assert len(wa.sent) == 2
    assert "REHLA Hoodie" in wa.sent[1].text and "الشنطة" in wa.sent[1].text


def test_never_more_than_two_per_silence(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11, cart=True)
    for hours in (0, 2, 3, 6, 12, 20):
        silence_nudges.check_silences(now=later(hours))
    assert len(wa.sent) == 2


# --- reset and skips --------------------------------------------------------


def test_her_reply_resets_the_counter_and_cancels_what_was_due(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11, cart=True)
    assert silence_nudges.check_silences() == 1

    customer_writes(minutes_ago=0)
    # Two hours after the *old* message is only minutes after the new one.
    assert silence_nudges.check_silences(now=later(0.05)) == 0
    assert row().first_state is None and row().second_state is None
    # ...and the new silence gets its own nudge #1.
    assert silence_nudges.check_silences(now=later(0.2)) == 1
    assert len(wa.sent) == 2


def test_an_order_placed_in_this_silence_ends_it(seeded, sender):
    from domain.models import Client, Order

    wa, _ = sender
    conversation(minutes_ago=11, cart=True)
    with session_scope() as db:
        client = Client(full_name="Test", phone=CUSTOMER)
        db.add(client)
        db.flush()
        db.add(
            Order(
                order_id="RH-T1", client_id=client.client_id, source_channel="whatsapp",
                source_external_id=CUSTOMER, shipping_address="x", contact_phone=CUSTOMER,
                governorate="Cairo", subtotal=1, shipping_fee=0, total=1, status="confirmed",
            )
        )
    assert silence_nudges.check_silences() == 0
    assert silence_nudges.check_silences(now=later(2)) == 0
    assert wa.sent == []


@pytest.mark.parametrize("closing", ["شكرا جدا", "تمام سلام", "لا مش عايزة خلاص", "thanks!"])
def test_nothing_after_she_closed_the_conversation(seeded, sender, closing):
    wa, _ = sender
    conversation(minutes_ago=11, customer=closing, reply="العفو، تحت أمرك في أي وقت", cart=True)
    silence_nudges.check_silences()
    silence_nudges.check_silences(now=later(2))
    assert wa.sent == []


@pytest.mark.parametrize(
    "text", ["السلام عليكم، عندكم توبات كم طويل؟", "وعليكم السلام، خلاص هاخده", "سلام عليكم"]
)
def test_a_greeting_or_a_yes_is_not_a_goodbye(text):
    assert not silence_nudges.closes_conversation(text)


def test_a_goodbye_still_is():
    assert silence_nudges.closes_conversation("تمام، سلام")
    assert silence_nudges.closes_conversation("السلام عليكم، شكرا جدا")


def test_a_chat_opened_with_a_greeting_is_nudged(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11, customer="السلام عليكم، عندكم توبات كم طويل؟")
    assert silence_nudges.check_silences() == 1
    assert len(wa.sent) == 1


def test_nothing_after_a_handoff(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11, cart=True)
    with session_scope() as db:
        identities.get(db, "whatsapp", CUSTOMER).paused_until_staff_reply = True
    silence_nudges.check_silences()
    silence_nudges.check_silences(now=later(2))
    assert wa.sent == []
    assert row().first_state == "skipped:handoff"


def test_nothing_after_a_staff_reply(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11, by="staff")
    silence_nudges.check_silences()
    assert wa.sent == []


def test_opting_out_outlives_the_silence(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=11, customer="stop متبعتليش رسايل تاني", cart=True)
    silence_nudges.check_silences()
    assert row().opted_out_at is not None

    customer_writes(minutes_ago=15)  # a later, ordinary message
    silence_nudges.check_silences()
    silence_nudges.check_silences(now=later(2))
    assert wa.sent == []


def test_never_outside_the_24_hour_window(seeded, sender):
    wa, _ = sender
    conversation(minutes_ago=25 * 60, cart=True)
    assert silence_nudges.check_silences() == 0
    assert wa.sent == []


# --- quiet hours ------------------------------------------------------------


def test_quiet_hours_drop_nudge_one(seeded, sender, monkeypatch):
    wa, _ = sender
    quiet_now(monkeypatch)
    conversation(minutes_ago=11)
    assert silence_nudges.check_silences() == 0
    assert wa.sent == []
    assert row().first_state == "skipped:quiet_hours"


def test_quiet_hours_defer_nudge_two_to_their_end(seeded, sender, monkeypatch):
    wa, _ = sender
    conversation(minutes_ago=11, cart=True)
    silence_nudges.check_silences()  # nudge #1, daytime
    quiet_now(monkeypatch)
    hour_from_now = later(2)
    # Due, but inside the (patched) quiet hour of *now*, not of +2h: put the
    # quiet window on the +2h hour instead.
    hour = hour_from_now.astimezone(silence_nudges.CAIRO).hour
    monkeypatch.setattr(
        silence_nudges, "settings",
        dataclasses.replace(settings, nudge_quiet_start_hour=hour, nudge_quiet_end_hour=(hour + 1) % 24),
    )
    assert silence_nudges.check_silences(now=hour_from_now) == 0
    assert row().second_state is None  # deferred, not dropped
    assert silence_nudges.check_silences(now=later(3.05)) == 1
    assert len(wa.sent) == 2


def test_quiet_hours_helpers():
    patched = dataclasses.replace(settings, nudge_quiet_start_hour=0, nudge_quiet_end_hour=9)
    original = silence_nudges.settings
    silence_nudges.settings = patched
    try:
        three_am = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)  # 04:00 Cairo (EEST)
        noon = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
        assert silence_nudges.in_quiet_hours(three_am)
        assert not silence_nudges.in_quiet_hours(noon)
        end = silence_nudges.quiet_hours_end(three_am).astimezone(silence_nudges.CAIRO)
        assert (end.hour, end.minute, end.day) == (9, 0, 2)
    finally:
        silence_nudges.settings = original
