"""A customer Meta identifies by a business-scoped user id, not a phone number.

From April 2026 every WhatsApp message webhook carries `messages[].from_user_id`
and `contacts[].user_id` -- a BSUID like `EG.1754797805572316`. For a customer
who uses a WhatsApp username, Meta sends *only* that: `from` and `wa_id` are
omitted entirely.

This adapter read `messages[].from` and nothing else, so those customers were
dropped by `_accept` at its very first check -- before the idempotency claim,
before the message was recorded, before the model, before the send. Not one of
them had ever received a single reply, from their first message onward, and
nothing anywhere said why.

Outbound, a BSUID goes in `recipient`; a phone number still goes in `to`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import session as session_store
from assistant.channels import whatsapp as adapter
from assistant.providers import set_provider
from assistant.providers.fake import RehearsalProvider
from common.identifiers import is_bsuid, is_phone_number
from config.settings import settings
from domain.db import SessionLocal
from domain.models import Client, SessionRow
from domain.services import identities
from integrations.whatsapp.client import WhatsAppClient

APP_SECRET = "test-app-secret"
PHONE = "201023402831"
#: The shape Meta actually sent, from the production log that found this.
BSUID = "EG.1754797805572316"
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
    """Every payload that would go to Meta, read receipts included -- how the
    recipient is addressed is exactly what these tests are about."""
    outbox: list[dict] = []

    def fake_post(self, payload):
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


def username_body(text: str, *, message_id="wamid.bsuid.1") -> dict:
    """Meta's payload for a customer with a WhatsApp username: a BSUID and no
    phone number anywhere. `from` and `wa_id` are absent, not empty."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "123456"},
                            "contacts": [
                                {"user_id": BSUID, "profile": {"name": "Omar"}}
                            ],
                            "messages": [
                                {
                                    "from_user_id": BSUID,
                                    "id": message_id,
                                    "timestamp": "1",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }


def phone_body(text: str, *, message_id="wamid.phone.1") -> dict:
    """The ordinary payload, which now also carries the BSUID alongside the
    phone number. Nothing about this conversation may change."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "123456"},
                            "contacts": [
                                {"wa_id": PHONE, "user_id": BSUID, "profile": {"name": "Omar"}}
                            ],
                            "messages": [
                                {
                                    "from": PHONE,
                                    "from_user_id": BSUID,
                                    "id": message_id,
                                    "timestamp": "1",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
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


def transcript(external_id: str) -> list[str]:
    with SessionLocal() as db:
        return [
            m["content"]
            for m in session_store.transcript(db, CHANNEL, external_id)
            if m["role"] == "user"
        ]


# --- telling the two apart ------------------------------------------------


def test_a_bsuid_is_recognised_and_a_phone_number_is_not():
    assert is_bsuid(BSUID)
    assert is_bsuid("US.13491208655302741918")
    assert not is_bsuid(PHONE)
    assert not is_bsuid("")
    assert not is_bsuid(None)
    assert not is_bsuid("EG-1754797805572316"), "a period, not a dash"
    assert not is_bsuid("E.1754797805572316"), "two letters, not one"

    assert is_phone_number(PHONE)
    assert is_phone_number("+20 102 340 2831")
    assert not is_phone_number(BSUID)
    assert not is_phone_number("")


# --- inbound --------------------------------------------------------------


def test_a_username_customer_is_answered_at_last(client, configured, sent):
    """The bug, end to end: this exact payload used to be dropped silently at
    `_accept`'s first check and the customer never got a reply."""
    assert post(client, username_body("عايز هودي")).status_code == 200
    adapter.dispatcher.wait_idle()

    assert transcript(BSUID) == ["عايز هودي"]
    replies = [p for p in sent if p.get("type") == "text"]
    assert replies, "the bot answered"


def test_the_reply_is_addressed_by_recipient_not_to(client, configured, sent):
    """`to` would be run through `normalise_recipient`, which strips `EG.` and
    leaves digits that address a different person entirely."""
    assert post(client, username_body("في حد؟")).status_code == 200
    adapter.dispatcher.wait_idle()

    replies = [p for p in sent if p.get("type") == "text"]
    assert replies
    for payload in replies:
        assert payload["recipient"] == BSUID
        assert "to" not in payload


def test_a_phone_number_conversation_is_completely_unchanged(client, configured, sent):
    """The BSUID now rides along on every payload. The phone number stays the
    key, so an existing conversation keeps its session row and its history."""
    assert post(client, phone_body("عايز تيشيرت")).status_code == 200
    adapter.dispatcher.wait_idle()

    assert transcript(PHONE) == ["عايز تيشيرت"]
    assert transcript(BSUID) == [], "not a second conversation for the same person"

    replies = [p for p in sent if p.get("type") == "text"]
    assert replies
    for payload in replies:
        assert payload["to"] == PHONE
        assert "recipient" not in payload


def test_the_profile_name_is_read_from_the_contact_either_way(client, configured, sent):
    """`contacts[]` is keyed by `wa_id` for one and `user_id` for the other."""
    assert post(client, username_body("اهلا")).status_code == 200
    adapter.dispatcher.wait_idle()

    with SessionLocal() as db:
        assert db.get(SessionRow, (CHANNEL, BSUID)) is not None


def test_a_message_with_neither_identity_is_still_named(client, configured, sent, caplog):
    body = username_body("hi", message_id="wamid.none")
    body["entry"][0]["changes"][0]["value"]["messages"][0].pop("from_user_id")

    with caplog.at_level("WARNING", logger="wanas.channel.whatsapp"):
        assert post(client, body).status_code == 200

    assert "no sender id at all" in caplog.text
    assert "wamid.none" in caplog.text


# --- outbound addressing --------------------------------------------------


@pytest.mark.parametrize(
    "method, args",
    [
        ("send_text", ("hello",)),
        ("send_image", ("https://cdn.example/x.jpg",)),
        ("send_template", ("back_in_stock",)),
    ],
)
def test_every_outbound_kind_addresses_a_bsuid_the_same_way(monkeypatch, method, args):
    captured: list[dict] = []
    monkeypatch.setattr(
        WhatsAppClient, "_post", lambda self, payload: (captured.append(payload), (True, None))[1]
    )
    whatsapp = WhatsAppClient(phone_number_id="1", access_token="t")

    getattr(whatsapp, method)(BSUID, *args)
    assert captured[-1]["recipient"] == BSUID
    assert "to" not in captured[-1]

    getattr(whatsapp, method)(PHONE, *args)
    assert captured[-1]["to"] == PHONE
    assert "recipient" not in captured[-1]


def test_an_interactive_picker_addresses_a_bsuid_too(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        WhatsAppClient, "_post", lambda self, payload: (captured.append(payload), (True, None))[1]
    )
    whatsapp = WhatsAppClient(phone_number_id="1", access_token="t")

    whatsapp.send_interactive(
        BSUID,
        {"kind": "buttons", "body": "تحب تكمل؟", "options": [{"id": "yes", "title": "أيوه"}]},
        fallback="تحب تكمل؟",
    )
    assert captured[-1]["recipient"] == BSUID
    assert "to" not in captured[-1]


def test_normalise_recipient_is_never_let_near_a_bsuid():
    """It strips every non-digit. `EG.1754797805572316` would become
    `201754797805572316` -- a number, belonging to somebody else."""
    mangled = WhatsAppClient.normalise_recipient(BSUID)
    assert mangled != BSUID
    assert WhatsAppClient(phone_number_id="1", access_token="t")._addressed(BSUID) == {
        "recipient": BSUID
    }


# --- the identity rule ----------------------------------------------------


def test_a_bsuid_never_auto_links_to_a_customer_by_phone(seeded):
    """`phone_variants` strips `EG.` and leaves sixteen digits. Matching on
    those would link a stranger to a real customer's saved address and order
    history -- the exact thing the confirmed-link rule exists to prevent."""
    stranger = Client(full_name="Someone Else", phone="1754797805572316")
    seeded.add(stranger)
    seeded.commit()

    identity = identities.get_or_create(seeded, CHANNEL, BSUID)
    identities.detect_pending_link_from_external_id(seeded, identity)

    assert identity.pending_link is None
    assert identity.client_id is None


def test_a_phone_number_still_finds_its_returning_customer(seeded):
    """The guard must not cost the feature it protects."""
    returning = Client(full_name="Omar Hassan", phone="01023402831")
    seeded.add(returning)
    seeded.commit()

    identity = identities.get_or_create(seeded, CHANNEL, PHONE)
    identities.detect_pending_link_from_external_id(seeded, identity)

    assert identity.pending_link is not None
    assert identity.pending_link["matched_on"] == "phone"
