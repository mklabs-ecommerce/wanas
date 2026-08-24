"""The Bug 1 guardrails.

A paused conversation must be loud, a crashed turn must give its claim back,
poisoned history must not kill a turn, and an operator must be able to
release a stuck conversation by hand. Each test pins one of those.
"""

from __future__ import annotations

import dataclasses
import json
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from assistant import session as session_store
from assistant.channels import whatsapp as adapter
from assistant.dispatcher import Pending
from assistant.providers.fake import RehearsalProvider
from assistant.runtime import RuntimeReply, claim_message, handle_message, release_claims
from assistant.tools.support_tools import raise_handoff
from config.settings import settings
from domain.db import SessionLocal
from domain.models import ChannelIdentity, SessionRow, StaffQueueItem, WebhookEvent
from domain.services import identities

CHANNEL = "whatsapp"
WHO = "201066976593"


@contextmanager
def fresh_db():
    """A separate connection, for rows written/committed outside this test's
    own session (the claim and its release both commit on their own)."""
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture()
def wa_client(seeded, monkeypatch):
    """The WhatsApp router with credentials on and Meta mocked out."""
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(
            settings,
            whatsapp_phone_number_id="123456",
            whatsapp_access_token="test-token",
        ),
    )
    monkeypatch.setattr(adapter.WhatsAppClient, "_post", lambda self, payload: (True, None))
    set_provider = RehearsalProvider()
    from assistant.providers import set_provider as _set

    _set(set_provider)
    app = FastAPI()
    app.include_router(adapter.router)
    try:
        yield TestClient(app)
    finally:
        _set(None)


def wa_body(message_id: str, *, message_type: str = "text") -> dict:
    content: dict = {"from": WHO, "id": message_id, "type": message_type, "timestamp": "1"}
    if message_type == "text":
        content["text"] = {"body": "مرحبا"}
    elif message_type == "audio":
        content["audio"] = {"id": "audio-9"}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": WHO, "profile": {"name": "Omar"}}],
                            "messages": [content],
                        },
                    }
                ],
            }
        ],
    }


# --- (a) the pause drop is loud -------------------------------------------


def test_a_paused_conversation_logs_a_warning_and_still_stores_the_message(seeded, caplog):
    """A human may legitimately be handling the chat -- but the drop can never
    again be indistinguishable from the bot ignoring one number."""
    identities.pause(seeded, CHANNEL, WHO)
    seeded.commit()

    with caplog.at_level("WARNING", logger="wanas.runtime"):
        reply = handle_message(CHANNEL, WHO, "still there?", db=seeded, provider=RehearsalProvider())

    assert reply.paused is True
    assert not reply.text  # nothing goes out
    assert "paused" in caplog.text and CHANNEL in caplog.text and WHO in caplog.text
    # The message is stored so staff keep the full thread.
    stored = session_store.load(seeded, CHANNEL, WHO)
    assert any(m.get("content") == "still there?" for m in stored)


def test_the_pause_warning_says_how_long(seeded, caplog):
    raise_handoff(seeded, CHANNEL, WHO, "customer_asked", "wants a human")
    seeded.commit()

    with caplog.at_level("WARNING", logger="wanas.runtime"):
        handle_message(CHANNEL, WHO, "hello?", db=seeded, provider=RehearsalProvider())

    # The newest handoff item is what dates the pause -- reason included.
    assert "customer_asked" in caplog.text
    assert "HO-" in caplog.text


# --- (b) a crashed turn releases its claim ---------------------------------


def _crash(*_args, **_kwargs):
    raise RuntimeError("turn crashed")


def test_a_crashed_turn_releases_the_claim_so_it_can_be_claimed_again(seeded, monkeypatch):
    assert claim_message("wamid.crash") is True
    monkeypatch.setattr(adapter, "handle_message", _crash)

    with pytest.raises(RuntimeError):
        adapter._deliver(WHO, Pending(texts=["hi"], text_ids=["wamid.crash"]))

    with fresh_db() as check:
        assert check.get(WebhookEvent, "wamid.crash") is None
    # The id is free again: a Meta retry gets processed instead of suppressed.
    assert claim_message("wamid.crash") is True


def test_a_crashed_ingest_releases_the_claim_so_the_retry_is_processed(wa_client, seeded, monkeypatch):
    """The first copy dies inside `_accept`, after the claim; Meta's retry of
    the same id must still be handled."""
    downloads = []
    monkeypatch.setattr(
        adapter.WhatsAppClient,
        "download_media",
        lambda self, media_id, destination_dir, **k: downloads.append(media_id)
        or (_ for _ in ()).throw(RuntimeError("media fetch exploded")),
    )

    # First delivery: claimed, then crashed inside `_accept`.
    assert wa_client.post("/webhooks/whatsapp", json=wa_body("wamid.retry", message_type="audio")).status_code == 200
    with fresh_db() as check:
        assert check.get(WebhookEvent, "wamid.retry") is None  # released, not eaten

    # Meta retries the same id -- this time the media download works.
    monkeypatch.setattr(
        adapter.WhatsAppClient,
        "download_media",
        lambda self, media_id, destination_dir, **k: f"data/inbound/{media_id}.ogg",
    )
    captured = []
    monkeypatch.setattr(adapter.dispatcher, "submit", lambda key, item: captured.append(item))
    assert wa_client.post("/webhooks/whatsapp", json=wa_body("wamid.retry", message_type="audio")).status_code == 200

    assert len(captured) == 1
    assert captured[0].audio_paths == ["data/inbound/audio-9.ogg"]


def test_a_successful_turn_keeps_the_claim(seeded, monkeypatch):
    """Idempotency is untouched: a handled message still cannot be processed
    twice."""
    assert claim_message("wamid.ok") is True

    monkeypatch.setattr(adapter, "handle_message", lambda *a, **k: RuntimeReply(text="all good"))
    monkeypatch.setattr(adapter.WhatsAppClient, "download_media", lambda self, *a, **k: None)

    adapter._deliver(WHO, Pending(texts=["hi"], text_ids=["wamid.ok"]))

    with fresh_db() as check:
        assert check.get(WebhookEvent, "wamid.ok") is not None
    assert claim_message("wamid.ok") is False


# --- (c) poisoned history cannot kill a turn -------------------------------


def test_malformed_history_does_not_raise_and_the_turn_still_replies(seeded, caplog):
    """A row written outside the app (manual edit, restore, SQLite fallback)
    used to raise on every load and silence the customer forever."""
    session_store.save(seeded, CHANNEL, WHO, [])
    seeded.commit()
    # Bypass the JSON type entirely, the way an out-of-app write would.
    seeded.execute(
        text("UPDATE sessions SET history = '{not json' WHERE channel = :c AND external_id = :e"),
        {"c": CHANNEL, "e": WHO},
    )
    seeded.commit()
    seeded.expire_all()  # force the next attribute access to re-read the poison

    with caplog.at_level("ERROR", logger="wanas.session"):
        reply = handle_message(CHANNEL, WHO, "categories", db=seeded, provider=RehearsalProvider())

    assert reply.text  # the turn produced a reply anyway
    assert "unreadable history" in caplog.text and WHO in caplog.text

    # The next save rewrote the row as valid JSON; persistence continues and
    # the conversation is no longer stuck.
    row = seeded.get(SessionRow, (CHANNEL, WHO))
    json.loads(json.dumps(row.history))  # round-trips: valid JSON
    assert any(m.get("role") == "user" for m in row.history)


def test_load_leaves_the_poisoned_value_in_place(seeded, caplog):
    """The fallback must not silently delete the stored row."""
    seeded.execute(
        text(
            "INSERT INTO sessions (channel, external_id, history, updated_at) "
            "VALUES (:c, :e, '{not json', CURRENT_TIMESTAMP)"
        ),
        {"c": CHANNEL, "e": WHO},
    )
    seeded.commit()
    seeded.expire_all()

    with caplog.at_level("ERROR", logger="wanas.session"):
        assert session_store.load(seeded, CHANNEL, WHO) == []

    raw = seeded.execute(
        text("SELECT history FROM sessions WHERE channel = :c AND external_id = :e"),
        {"c": CHANNEL, "e": WHO},
    ).scalar_one()
    assert raw == "{not json"  # exactly as it was


# --- (d) the operator escape hatch -----------------------------------------


def test_cli_inspect_shows_a_stuck_conversation(seeded, capsys):
    import manage as cli

    identities.pause(seeded, CHANNEL, WHO)
    raise_handoff(seeded, CHANNEL, WHO, "complaint", "damaged item")
    session_store.append(seeded, CHANNEL, WHO, {"role": "user", "content": "hi"})
    seeded.commit()
    seeded.expire_all()

    assert cli.main(["inspect-conversation", WHO]) == 0
    out = capsys.readouterr().out
    assert f"conversation {CHANNEL}/{WHO}" in out
    assert "paused_until_staff_reply: True" in out
    assert "open staff_queue items: 1" in out
    assert "HO-" in out
    assert "history messages: 1" in out

    assert cli.main(["inspect-conversation", "199999999999"]) == 1
    assert "no conversation" in capsys.readouterr().err


def test_cli_release_clears_the_pause_and_resolves_handoffs(seeded, capsys):
    import manage as cli

    identities.pause(seeded, CHANNEL, WHO)
    item = raise_handoff(seeded, CHANNEL, WHO, "customer_asked", "wants a human")
    seeded.commit()
    seeded.expire_all()

    assert cli.main(["release-conversation", WHO]) == 0
    out = capsys.readouterr().out
    assert "paused_until_staff_reply: True -> False" in out
    assert "resolved open handoff items: 1" in out

    seeded.expire_all()
    identity = seeded.get(ChannelIdentity, (CHANNEL, WHO))
    assert identity.paused_until_staff_reply is False
    resolved = seeded.get(StaffQueueItem, item.queue_id)
    assert resolved.status == "resolved"

    # And the next message is answered by the bot again, not dropped.
    reply = handle_message(CHANNEL, WHO, "categories", db=seeded, provider=RehearsalProvider())
    assert not reply.paused
    assert reply.text


def test_release_claims_is_a_no_op_without_ids():
    assert release_claims(None) == 0
    assert release_claims([]) == 0
    assert release_claims("") == 0
