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

from assistant.channels import whatsapp as adapter
from assistant.providers import set_provider
from assistant.providers.base import ModelReply
from assistant.providers.fake import RehearsalProvider, ScriptedProvider
from config.settings import settings
from domain.models import Order, QueueKind, ShippingRate
from domain.services import notifications, queues

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
        # Read receipts and typing indicators go through the same POST but are
        # not messages; the assertions below are about what the customer sees.
        if payload.get("status") != "read":
            outbox.append(payload)
        return True, None, "sent.1"

    def fake_upload(self, path):
        return f"media-for-{path}"

    def fake_download(self, media_id, destination_dir, *, default_extension=".jpg"):
        return f"data/inbound/{media_id}{default_extension}"

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


def webhook_body(
    text: str, *, message_id: str = "wamid.1", message_type: str = "text", reply_to: str | None = None
) -> dict:
    content: dict = {"from": PHONE, "id": message_id, "type": message_type, "timestamp": "1"}
    if message_type == "text":
        content["text"] = {"body": text}
    elif message_type == "image":
        content["image"] = {"id": "media-99", "mime_type": "image/jpeg", "caption": text}
    elif message_type == "audio":
        content["audio"] = {"id": "audio-1"}
    elif message_type == "location":
        content["location"] = {"latitude": 30.0, "longitude": 31.0}
    if reply_to:
        # Meta's "long-pressed reply to a specific earlier message" marker.
        content["context"] = {"id": reply_to}
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


def test_an_order_can_be_placed_over_the_webhook(client, configured, sent, seeded, monkeypatch):
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.commit()
    # Production wiring: the proactive confirmation leaves through the same
    # client the turn's own reply would, so `sent` is everything the customer's
    # phone actually receives.
    monkeypatch.setitem(notifications._senders, "whatsapp", adapter.WhatsAppClient())

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
    # Exactly one confirmation reaches the phone: the one the shop composed.
    # The turn itself sends nothing more -- a second, model-written "your
    # order is confirmed" for the same order is what this asserts against.
    bodies = [m.get("text", {}).get("body", "") for m in sent if m.get("type") == "text"]
    confirmations = [b for b in bodies if "تم تأكيد طلبك" in b or "WNS-1001" in b]
    assert len(confirmations) == 1, bodies
    assert "تم تأكيد طلبك" in confirmations[0]


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


def test_a_voice_note_no_one_can_transcribe_goes_to_a_person(client, configured, sent, seeded):
    """The rehearsal provider cannot listen, so this is the fallback path.

    It is its own handoff reason rather than `out_of_scope`: staff working the
    queue need to see that a message is waiting on someone to listen to it.
    """
    post(client, webhook_body("", message_type="audio"))
    seeded.expire_all()
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "voice_received"
    assert item.payload["audio"] == ["data/inbound/audio-1.ogg"]
    assert len(sent) == 1


def test_an_unsupported_message_type_goes_to_a_person(client, configured, sent, seeded):
    post(client, webhook_body("", message_type="location"))
    seeded.expire_all()
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "out_of_scope"
    assert item.payload["message_type"] == "location"
    assert len(sent) == 1


def test_accept_captures_the_reply_to_id_from_meta_context(configured, seeded, monkeypatch):
    """`context.id` is how Meta marks a long-pressed "reply to this specific
    earlier message". It must reach the Pending batch the dispatcher merges,
    or a reply to one of several photos has nothing to resolve against."""
    captured = []
    monkeypatch.setattr(adapter.dispatcher, "submit", lambda key, item: captured.append(item))
    monkeypatch.setattr(adapter.WhatsAppClient, "_post", lambda self, payload: (True, None, "sent.1"))

    message = {
        "from": PHONE,
        "id": "wamid.reply",
        "type": "text",
        "timestamp": "1",
        "text": {"body": "مقاس M لو سمحت"},
        "context": {"id": "wamid.photo1"},
    }
    adapter._accept(message, None)

    assert len(captured) == 1
    pending = captured[0]
    assert pending.reply_to == {"wamid.reply": "wamid.photo1"}
    assert pending.text_ids == ["wamid.reply"]


def test_accept_gives_each_image_its_own_id(configured, seeded, monkeypatch):
    captured = []
    monkeypatch.setattr(adapter.dispatcher, "submit", lambda key, item: captured.append(item))
    monkeypatch.setattr(adapter.WhatsAppClient, "_post", lambda self, payload: (True, None, "sent.1"))
    monkeypatch.setattr(adapter.WhatsAppClient, "download_media", lambda self, *a, **k: "data/inbound/x.jpg")

    message = {
        "from": PHONE,
        "id": "wamid.img1",
        "type": "image",
        "timestamp": "1",
        "image": {"id": "media-1"},
    }
    adapter._accept(message, None)

    assert captured[0].image_ids == ["wamid.img1"]


# --- reply-to annotation ----------------------------------------------------


def test_annotate_replies_labels_a_reply_to_a_photo():
    from assistant.dispatcher import Pending

    pending = Pending(
        image_paths=["data/inbound/a.jpg", "data/inbound/b.jpg"],
        image_ids=["wamid.img1", "wamid.img2"],
        texts=["a size M please"],
        text_ids=["wamid.txt1"],
        reply_to={"wamid.txt1": "wamid.img2"},
    )
    assert adapter._annotate_replies(pending) == "[replying to photo 2] a size M please"


def test_annotate_replies_labels_a_reply_to_a_voice_note():
    from assistant.dispatcher import Pending

    pending = Pending(
        audio_paths=["data/inbound/a.ogg"],
        audio_ids=["wamid.audio1"],
        texts=["نفس اللون بس مقاس تاني"],
        text_ids=["wamid.txt1"],
        reply_to={"wamid.txt1": "wamid.audio1"},
    )
    assert adapter._annotate_replies(pending) == "[replying to voice note 1] نفس اللون بس مقاس تاني"


def test_annotate_replies_leaves_a_reply_outside_the_batch_unannotated():
    """A reply to something from an already-answered earlier turn has
    nothing in this batch to resolve against -- left as plain text rather
    than a confusing dangling reference."""
    from assistant.dispatcher import Pending

    pending = Pending(
        texts=["زي اللي قلتلي عليه امبارح"],
        text_ids=["wamid.txt1"],
        reply_to={"wamid.txt1": "wamid.from-a-week-ago"},
    )
    assert adapter._annotate_replies(pending) == "زي اللي قلتلي عليه امبارح"


def test_annotate_replies_is_a_no_op_with_nothing_to_annotate():
    from assistant.dispatcher import Pending

    pending = Pending(texts=["عايز هودي", "أسود"])
    assert adapter._annotate_replies(pending) == pending.text


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


def test_a_shopify_hosted_photo_is_sent_by_link_not_uploaded(configured, monkeypatch):
    """catalog.get_variants can now hand back a Shopify CDN URL instead of a
    local file (domain/services/catalog.py::_overlay_images). Meta fetches a
    `link` itself, so a URL must never touch the local-file upload path --
    there is nothing on this process's disk to open."""
    uploads: list[str] = []
    posts: list[dict] = []

    def fake_upload(self, path):
        uploads.append(path)
        return "media-1"

    def fake_post(self, payload):
        posts.append(payload)
        return True, None, "sent.1"

    monkeypatch.setattr(adapter.WhatsAppClient, "_upload", fake_upload)
    monkeypatch.setattr(adapter.WhatsAppClient, "_post", fake_post)
    client = adapter.WhatsAppClient(phone_number_id="1", access_token="t")

    url = "https://cdn.shopify.com/s/files/1/hoodie-olive.jpg"
    result = client.send_image("201000000123", url, caption="زي كده")

    assert uploads == []  # never went near the upload endpoint
    assert result.delivered is True
    assert posts[0]["image"] == {"link": url, "caption": "زي كده"}


def test_a_local_photo_still_goes_through_the_cached_upload(configured, monkeypatch):
    """The other half of the same guard: a plain local path must not be
    mistaken for a link and sent unresolved."""
    uploads: list[str] = []

    def fake_upload(self, path):
        uploads.append(path)
        return "media-1"

    monkeypatch.setattr(adapter.WhatsAppClient, "_upload", fake_upload)
    monkeypatch.setattr(adapter.WhatsAppClient, "_post", lambda self, payload: (True, None, "sent.1"))
    client = adapter.WhatsAppClient(phone_number_id="1", access_token="t")

    client.send_image("201000000123", "data/images/wanas-hoodie/01.jpg")

    assert uploads == ["data/images/wanas-hoodie/01.jpg"]


def test_the_notification_sender_is_only_registered_when_configured(monkeypatch):
    from domain.services import notifications

    monkeypatch.setattr(adapter, "settings", dataclasses.replace(settings, whatsapp_access_token=""))
    assert adapter.register_outbound_sender() is False
    assert isinstance(notifications.get_sender(), notifications.LogSender)


def test_a_reply_to_something_the_bot_said_reaches_the_model_as_a_quote(
    client, configured, sent, seeded
):
    """End to end, the case the customer reported: the bot answers, they
    long-press that answer and reply to it, and the turn that follows knows
    which message they meant instead of guessing from recency.

    Nothing on our side used to know the bot's own message by the name
    WhatsApp quotes it under -- the send's response body was read for a status
    code and thrown away -- so `context.id` matched nothing at all.
    """
    provider = ScriptedProvider([ModelReply(text="عندنا أسود وأوليڤ وبيچ")])
    set_provider(provider)

    assert post(client, webhook_body("الهودي بيجي بألوان إيه؟")).status_code == 200

    provider.push(ModelReply(text="تمام، الأسود"))
    assert (
        post(
            client,
            webhook_body("الأول ده", message_id="wamid.2", reply_to="sent.1"),
        ).status_code
        == 200
    )

    _system, history, _tools = provider.calls[-1]
    assert "عندنا أسود وأوليڤ وبيچ" in history[-1]["content"]
    assert "الأول ده" in history[-1]["content"]
