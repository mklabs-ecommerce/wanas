"""A customer's message is in the dashboard before the bot has said anything.

Until this existed, `sessions` was written only by `agent.run_turn`, *after*
the model had answered. A conversation the bot was still thinking about, one
paused for a staff member who could not see it to release it, one whose turn
crashed and rolled its whole transaction back, and one the model simply never
replied to were all indistinguishable in the dashboard from a customer who had
never written at all. That is how numbers went unanswered without anyone
noticing.

So ingest records the message itself, in its own committed transaction, and
the turn folds that provisional copy into the real one when it runs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import runtime, session as session_store
from assistant.channels import whatsapp as adapter
from assistant.providers import set_provider
from assistant.providers.fake import RehearsalProvider
from config.settings import settings
from domain.db import SessionLocal
from domain.models import SessionRow
from domain.services import identities

APP_SECRET = "test-app-secret"
PHONE = "201000000123"
CHANNEL = "whatsapp"


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(
            settings,
            whatsapp_phone_number_id="123456",
            whatsapp_access_token="test-token",
            whatsapp_app_secret=APP_SECRET,
            whatsapp_verify_token="test-verify-token",
        ),
    )


@pytest.fixture()
def sent(monkeypatch):
    outbox: list[dict] = []

    def fake_post(self, payload):
        # Read receipts go through the same POST but are not messages.
        if payload.get("status") != "read":
            outbox.append(payload)
        return True, None

    monkeypatch.setattr(adapter.WhatsAppClient, "_post", fake_post)
    monkeypatch.setattr(adapter.WhatsAppClient, "_upload", lambda self, path: "media")
    return outbox


@pytest.fixture()
def client(seeded):
    set_provider(RehearsalProvider())
    app = FastAPI()
    app.include_router(adapter.router)
    try:
        yield TestClient(app)
    finally:
        set_provider(None)


def webhook_body(text: str, *, message_id="wamid.1", message_type="text", extra=None) -> dict:
    content: dict = {"from": PHONE, "id": message_id, "type": message_type, "timestamp": "1"}
    if message_type == "text":
        content["text"] = {"body": text}
    elif message_type == "location":
        content["location"] = {"latitude": 30.0, "longitude": 31.0}
    if extra:
        content.update(extra)
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": PHONE, "profile": {"name": "Omar"}}],
                            "messages": [content],
                        },
                    }
                ]
            }
        ],
    }


def post(client, body: dict):
    raw = json.dumps(body).encode()
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={"content-type": "application/json", "x-hub-signature-256": f"sha256={digest}"},
    )


def stored() -> list[dict]:
    with SessionLocal() as db:
        return session_store.transcript(db, CHANNEL, PHONE)


def texts() -> list[str]:
    return [m["content"] for m in stored() if m["role"] == "user"]


# --- the record itself ----------------------------------------------------


def test_the_message_is_stored_before_the_bot_answers(seeded):
    """`record_inbound` commits on its own, so nothing the turn does later --
    including rolling back -- can take the record away."""
    assert runtime.record_inbound(CHANNEL, PHONE, "عايز هودي", message_id="wamid.a")

    history = stored()
    assert [m["content"] for m in history] == ["عايز هودي"]
    assert history[0]["provisional"] == "wamid.a"


def test_a_crashing_turn_still_leaves_the_conversation_visible(
    client, configured, sent, monkeypatch
):
    """The exact failure that hid the unanswered numbers: `handle_message`
    raises, its `session_scope` rolls back, and the dashboard used to show
    nothing at all for a customer who definitely wrote."""

    def explode(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(adapter, "handle_message", explode)

    assert post(client, webhook_body("في حد؟")).status_code == 200
    adapter.dispatcher.wait_idle()

    assert texts() == ["في حد؟"], "the customer's words survived the crash"


def test_a_paused_conversation_is_visible_from_the_very_first_message(client, configured, sent):
    """A latched pause means the bot never replies until staff release it. If
    the conversation is invisible, nobody ever does -- which is what 'never
    answered, not even once' looks like from the customer's side."""
    with SessionLocal() as db:
        identity = identities.get_or_create(db, CHANNEL, PHONE)
        identity.paused_until_staff_reply = True
        db.commit()

    assert post(client, webhook_body("السلام عليكم")).status_code == 200
    adapter.dispatcher.wait_idle()

    assert texts() == ["السلام عليكم"]
    assert sent == [], "paused: the bot said nothing, and that is correct"


def test_the_turn_does_not_store_the_message_twice(client, configured, sent):
    """The provisional copy is folded into the real one the turn writes."""
    assert post(client, webhook_body("عايز تيشيرت")).status_code == 200
    adapter.dispatcher.wait_idle()

    assert texts() == ["عايز تيشيرت"]
    assert not any(m.get("provisional") for m in stored()), "the flag is cleared by the turn"
    assert sent, "and the bot still replied"


def test_an_unanswered_message_keeps_its_provisional_mark(seeded):
    """A batch that never ran leaves its record behind, flag and all -- that
    row is the only evidence the customer wrote and got nothing back. A later
    turn for a *different* message must not quietly tidy it away."""
    runtime.record_inbound(CHANNEL, PHONE, "أول رسالة", message_id="wamid.lost")

    with SessionLocal() as db:
        runtime.handle_message(CHANNEL, PHONE, "تاني رسالة", db=db, recorded_ids={"wamid.other"})
        db.commit()

    history = stored()
    assert [m["content"] for m in history if m["role"] == "user"] == ["أول رسالة", "تاني رسالة"]
    assert history[0]["provisional"] == "wamid.lost"


# --- the paths that used to vanish ---------------------------------------


def test_a_message_type_with_no_handler_is_recorded_not_dropped(client, configured, sent, caplog):
    """`reaction`, `system`, anything Meta adds later. No reply -- but it is
    named at WARNING with the number on it, and it shows in the dashboard."""
    with caplog.at_level("WARNING", logger="wanas.channel.whatsapp"):
        assert post(client, webhook_body("", message_type="reaction")).status_code == 200
    adapter.dispatcher.wait_idle()

    assert texts() == ["[reaction]"]
    assert PHONE in caplog.text
    assert sent == []


def test_a_template_button_tap_is_answered_rather_than_ignored(client, configured, sent):
    """A tapped quick reply on a template arrives as type `button`. It used to
    fall through to the silent drop, so a conversation that opened with one
    got no answer at all."""
    body = webhook_body(
        "", message_type="button", extra={"button": {"text": "نعم", "payload": "yes"}}
    )
    assert post(client, body).status_code == 200
    adapter.dispatcher.wait_idle()

    assert texts() == ["نعم"]
    assert sent, "a button tap is a real message and gets a real reply"


def test_an_unsupported_type_is_recorded_alongside_its_handoff(client, configured, sent):
    """A location goes to a person. The customer's turn is still in the
    transcript, so the staff member handling it can see there was one."""
    assert post(client, webhook_body("", message_type="location")).status_code == 200
    adapter.dispatcher.wait_idle()

    assert texts() == ["[location]"]
    assert sent and adapter.UNSUPPORTED_ACK in sent[0]["text"]["body"]


# --- it must never make things worse -------------------------------------


def test_recording_never_raises(monkeypatch, seeded):
    """A failure to record must not become a failure to answer."""

    def explode(*args, **kwargs):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(session_store, "append", explode)
    assert runtime.record_inbound(CHANNEL, PHONE, "hi", message_id="wamid.q") is False


def test_nothing_is_recorded_for_an_empty_message(seeded):
    assert runtime.record_inbound(CHANNEL, PHONE, "   ", message_id="wamid.e") is False
    with SessionLocal() as db:
        assert db.get(SessionRow, (CHANNEL, PHONE)) is None
