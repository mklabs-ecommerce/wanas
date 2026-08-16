"""The WhatsApp adapter.

The adapter is the only thing that differs from the local harness, so these
tests cover exactly that: the handshake, the signature, idempotency, and
turning a Meta payload into the same `handle_message` call.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings
from backend.models import Order, QueueKind, ShippingRate
from backend.services import queues
from chatbot.channels import whatsapp as adapter
from chatbot.providers import set_provider
from chatbot.providers.fake import RehearsalProvider

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
PHONE = "201000000123"


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
            whatsapp_verify_token=VERIFY_TOKEN,
        ),
    )


@pytest.fixture()
def sent(monkeypatch):
    """Capture what would go to Meta instead of making a network call."""
    outbox: list[dict] = []

    def fake_post(self, payload):
        outbox.append(payload)
        return True, None

    def fake_upload(self, path):
        return f"media-for-{path}"

    def fake_download(self, media_id, destination_dir):
        return f"data/inbound/{media_id}.jpg"

    monkeypatch.setattr(adapter.WhatsAppClient, "_post", fake_post)
    monkeypatch.setattr(adapter.WhatsAppClient, "_upload", fake_upload)
    monkeypatch.setattr(adapter.WhatsAppClient, "download_media", fake_download)
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


def webhook_body(text: str, *, message_id: str = "wamid.1", message_type: str = "text") -> dict:
    content: dict = {"from": PHONE, "id": message_id, "type": message_type, "timestamp": "1"}
    if message_type == "text":
        content["text"] = {"body": text}
    elif message_type == "image":
        content["image"] = {"id": "media-99", "mime_type": "image/jpeg", "caption": text}
    elif message_type == "audio":
        content["audio"] = {"id": "audio-1"}
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
                            "contacts": [{"wa_id": PHONE, "profile": {"name": "Omar"}}],
                            "messages": [content],
                        },
                    }
                ],
            }
        ],
    }


def post(client, body: dict, *, secret: str | None = APP_SECRET):
    raw = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if secret is not None:
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        headers["x-hub-signature-256"] = f"sha256={digest}"
    return client.post("/webhooks/whatsapp", content=raw, headers=headers)


# --- configuration gate ---------------------------------------------------


def test_webhook_is_inert_without_credentials(client):
    """Nothing half-works: with no Meta credentials the endpoint refuses and
    every other path in the system is unaffected."""
    assert post(client, webhook_body("hi")).status_code == 503


# --- handshake and signature ---------------------------------------------


def test_verify_handshake(client, configured):
    response = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "12345"},
    )
    assert response.status_code == 200
    assert response.text == "12345"


def test_verify_handshake_rejects_a_wrong_token(client, configured):
    response = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )
    assert response.status_code == 403


def test_bad_signature_is_rejected(client, configured, sent):
    assert post(client, webhook_body("هاي"), secret="wrong-secret").status_code == 403
    assert post(client, webhook_body("هاي"), secret=None).status_code == 403
    assert sent == []


def test_signature_helper():
    body = b'{"a":1}'
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert adapter.verify_signature(APP_SECRET, body, f"sha256={digest}") is True
    assert adapter.verify_signature(APP_SECRET, body, f"sha256={'0' * 64}") is False
    assert adapter.verify_signature(APP_SECRET, body, None) is False


# --- inbound --------------------------------------------------------------


def test_text_message_reaches_the_same_entry_point_and_gets_a_reply(client, configured, sent):
    assert post(client, webhook_body("categories")).status_code == 200
    assert len(sent) == 1
    assert sent[0]["type"] == "text"
    assert sent[0]["to"] == PHONE
    assert "T-Shirts" in sent[0]["text"]["body"]


def test_a_retried_delivery_does_not_answer_twice(client, configured, sent):
    body = webhook_body("categories", message_id="wamid.same")
    post(client, body)
    post(client, body)
    assert len(sent) == 1


def test_an_order_can_be_placed_over_the_webhook(client, configured, sent, seeded):
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.commit()

    for index, text in enumerate(
        [
            "add wanas-hoodie-s-olive 1",
            "order Omar | Cairo | 5 Test Street | 01000000123",
        ]
    ):
        assert post(client, webhook_body(text, message_id=f"wamid.{index}")).status_code == 200

    seeded.expire_all()
    order = seeded.get(Order, "WNS-1001")
    assert order is not None
    assert order.source_channel == "whatsapp"
    assert float(order.total) == 710
    assert "WNS-1001" in sent[-1]["text"]["body"]


def test_a_sizing_question_sends_the_chart_as_an_image_message(client, configured, sent):
    post(client, webhook_body("size wanas-sweatpant"))
    kinds = [m["type"] for m in sent]
    assert kinds == ["text", "image"]
    # A real image message, not a link.
    assert sent[1]["image"]["id"] == "media-for-data/size-charts/wide-leg-sweatpants.png"
    # The text still carries the numbers for anyone who never opens it.
    assert "31" in sent[0]["text"]["body"]


def test_an_incoming_photo_goes_to_the_handoff_queue_with_the_photo(client, configured, sent, seeded):
    post(client, webhook_body("زي كده", message_type="image"))

    seeded.expire_all()
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "image_received"
    assert item.payload["images"] == ["data/inbound/media-99.jpg"]
    # The customer is told a person is looking, and the bot never describes it.
    assert len(sent) == 1
    assert "الفريق" in sent[0]["text"]["body"]


def test_an_unsupported_message_type_goes_to_a_person(client, configured, sent, seeded):
    post(client, webhook_body("", message_type="audio"))
    seeded.expire_all()
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "out_of_scope"
    assert item.payload["message_type"] == "audio"
    assert len(sent) == 1


def test_a_paused_conversation_gets_no_reply(client, configured, sent, seeded):
    post(client, webhook_body("human customer_asked عايز أكلم حد", message_id="wamid.a"))
    sent.clear()
    post(client, webhook_body("categories", message_id="wamid.b"))
    assert sent == []


# --- outbound helpers -----------------------------------------------------


def test_recipient_normalisation():
    normalise = adapter.WhatsAppClient.normalise_recipient
    assert normalise("01001234567") == "201001234567"
    assert normalise("+20 100 123 4567") == "201001234567"
    assert normalise("201001234567") == "201001234567"
    assert normalise("0020 1001234567") == "201001234567"


def test_media_ids_are_cached_not_re_uploaded(configured, monkeypatch, seeded):
    uploads: list[str] = []

    def fake_upload(self, path):
        uploads.append(path)
        return "media-1"

    monkeypatch.setattr(adapter.WhatsAppClient, "_upload", fake_upload)
    client = adapter.WhatsAppClient(phone_number_id="1", access_token="t")
    assert client.media_id_for("data/size-charts/zipup.png") == "media-1"
    assert client.media_id_for("data/size-charts/zipup.png") == "media-1"
    assert uploads == ["data/size-charts/zipup.png"]


def test_the_notification_sender_is_only_registered_when_configured(monkeypatch):
    from backend.services import notifications

    monkeypatch.setattr(adapter, "settings", dataclasses.replace(settings, whatsapp_access_token=""))
    assert adapter.register_outbound_sender() is False
    assert isinstance(notifications.get_sender(), notifications.LogSender)
