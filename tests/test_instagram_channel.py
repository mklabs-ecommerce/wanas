"""The Instagram DM adapter -- STEP 6, "DMs work end to end".

Mirrors tests/test_whatsapp_channel.py because the adapter mirrors the
WhatsApp adapter: the assertions are about what differs at the edge -- the
handshake, the signature (under the *Instagram* app secret), the Messenger-
shaped envelope, the self-drop rules, and the `ig:` idempotency prefix --
and about the one thing that must never happen: a reply delivered through
the WhatsApp client.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot.channels import instagram as adapter
from chatbot.channels import whatsapp as whatsapp_adapter
from chatbot.providers import set_provider
from chatbot.providers.fake import RehearsalProvider
from config.settings import settings
from domain.db import SessionLocal
from domain.models import Channel, QueueKind, WebhookEvent
from domain.services import queues

APP_SECRET = "ig-test-app-secret"
VERIFY_TOKEN = "ig-verify-token"
IG_ID = "17841400000000000"  # the shop's own professional-account id
IGSID = "98765432109876543"  # a customer


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(
            settings,
            instagram_account_id=IG_ID,
            instagram_access_token="test-token",
            instagram_app_secret=APP_SECRET,
            instagram_verify_token=VERIFY_TOKEN,
        ),
    )


@pytest.fixture()
def sent(monkeypatch):
    """Capture what would go to Meta instead of making a network call."""
    outbox: list[dict] = []

    def fake_post(self, payload):
        # mark_seen / typing_on go through the same POST but are not messages;
        # these assertions are about what the customer sees.
        if "sender_action" not in payload:
            outbox.append(payload)
        return True, None

    monkeypatch.setattr(adapter.InstagramClient, "_post", fake_post)
    return outbox


@pytest.fixture()
def whatsapp_outbox(monkeypatch):
    """Everything that would have gone out over WhatsApp. For this channel it
    must stay empty -- always."""
    outbox: list[dict] = []

    def fake_post(self, payload):
        if payload.get("status") != "read":
            outbox.append(payload)
        return True, None

    monkeypatch.setattr(whatsapp_adapter.WhatsAppClient, "_post", fake_post)
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
    text: str,
    *,
    mid: str = "aWdfZG1lc3NhZ2U6MA==",
    sender: str = IGSID,
    message_extra: dict | None = None,
) -> dict:
    message: dict = {"mid": mid, "text": text}
    if message_extra:
        message.update(message_extra)
    return {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ID,
                "time": 1700000000,
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": IG_ID},
                        "timestamp": 1700000000,
                        "message": message,
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
    return client.post("/webhooks/instagram", content=raw, headers=headers)


# --- configuration gate ---------------------------------------------------


def test_webhook_is_inert_without_credentials(client):
    assert post(client, webhook_body("hi")).status_code == 503
    assert client.post("/webhooks/instagram", content=b"{}").status_code == 503


# --- handshake and signature ---------------------------------------------


def test_verify_handshake(client, configured):
    response = client.get(
        "/webhooks/instagram",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "12345"},
    )
    assert response.status_code == 200
    assert response.text == "12345"


def test_verify_handshake_rejects_a_wrong_token(client, configured):
    response = client.get(
        "/webhooks/instagram",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )
    assert response.status_code == 403


def test_verify_handshake_with_no_token_configured_is_503(client, monkeypatch):
    monkeypatch.setattr(
        adapter, "settings", dataclasses.replace(settings, instagram_verify_token="")
    )
    response = client.get(
        "/webhooks/instagram",
        params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "12345"},
    )
    assert response.status_code == 503


def test_bad_signature_is_rejected(client, configured, sent):
    assert post(client, webhook_body("هاي"), secret="wrong-secret").status_code == 403
    # Missing header entirely, with a secret configured.
    assert post(client, webhook_body("هاي"), secret=None).status_code == 403
    assert sent == []


def test_signature_is_checked_against_the_instagram_secret_not_whatsapps(
    client, configured, sent
):
    body = webhook_body("هاي")
    raw = json.dumps(body).encode()
    whatsapp_digest = hmac.new(b"a-whatsapp-secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/instagram",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": f"sha256={whatsapp_digest}",
        },
    )
    assert response.status_code == 403
    assert sent == []


# --- inbound --------------------------------------------------------------


def test_a_text_message_runs_a_turn_and_replies_to_the_right_igsid(
    client, configured, sent, whatsapp_outbox
):
    assert post(client, webhook_body("categories")).status_code == 200
    assert len(sent) == 1
    payload = sent[0]
    assert payload["recipient"] == {"id": IGSID}
    assert payload["message"]["text"]
    # And none of it went anywhere near the WhatsApp client.
    assert whatsapp_outbox == []


def test_sender_actions_fire_but_are_not_messages(client, configured, sent, monkeypatch):
    actions: list[str] = []

    real_post = sent  # noqa: F841 -- keep the recording patch installed
    def fake_post(self, payload):
        if "sender_action" in payload:
            actions.append(payload["sender_action"])
            return True, None
        sent.append(payload)
        return True, None

    monkeypatch.setattr(adapter.InstagramClient, "_post", fake_post)
    post(client, webhook_body("categories"))

    assert actions == ["mark_seen", "typing_on"]


def test_an_echo_never_reaches_anything(client, configured, sent, seeded):
    body = webhook_body("categories", message_extra={"is_echo": True})
    assert post(client, body).status_code == 200
    assert sent == []
    with SessionLocal() as db:
        assert db.query(WebhookEvent).count() == 0  # no claim either


def test_a_deleted_or_unsupported_message_event_is_dropped(client, configured, sent, seeded):
    assert post(client, webhook_body("x", message_extra={"is_deleted": True})).status_code == 200
    assert post(client, webhook_body("x", message_extra={"is_unsupported": True})).status_code == 200
    assert sent == []
    with SessionLocal() as db:
        assert db.query(WebhookEvent).count() == 0


def test_the_shop_s_own_message_never_gets_answered(client, configured, sent, seeded):
    """The single worst failure on this channel: the bot answering itself,
    forever. `sender.id` equal to the account id drops before the claim."""
    body = webhook_body("categories", sender=IG_ID)
    assert post(client, body).status_code == 200
    assert sent == []
    with SessionLocal() as db:
        assert db.query(WebhookEvent).count() == 0


def test_the_same_mid_delivered_twice_is_answered_once_under_a_prefixed_claim(
    client, configured, sent
):
    body = webhook_body("categories", mid="aWdfZG91YmxpY2F0ZQ==")
    post(client, body)
    post(client, body)
    assert len(sent) == 1

    with SessionLocal() as db:
        rows = db.query(WebhookEvent).all()
    assert len(rows) == 1
    assert rows[0].platform_message_id.startswith("ig:")
    assert rows[0].platform_message_id.endswith("aWdfZG91YmxpY2F0ZQ==")


def test_the_reply_goes_through_the_registered_instagram_sender_not_whatsapps(
    configured, monkeypatch, seeded
):
    """The registry contract, exercised through the boot path: after
    `register_outbound_sender`, the Notification service's instagram_dm key
    holds an InstagramClient and the whatsapp key still holds WhatsApp's."""
    from domain.services import notifications

    notifications.register_sender(notifications.LogSender(), channel="whatsapp")
    try:
        assert adapter.register_outbound_sender() is True
        assert isinstance(notifications.get_sender(Channel.INSTAGRAM_DM.value), adapter.InstagramClient)
        assert isinstance(notifications.get_sender("whatsapp"), notifications.LogSender)
    finally:
        notifications.register_sender(notifications.LogSender(), channel=Channel.INSTAGRAM_DM.value)


def test_register_outbound_sender_is_inert_when_unconfigured(monkeypatch):
    from domain.services import notifications

    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(settings, instagram_account_id="", instagram_access_token=""),
    )
    assert adapter.register_outbound_sender() is False
    assert not isinstance(
        notifications.get_sender(Channel.INSTAGRAM_DM.value), adapter.InstagramClient
    )


# --- attachments (STEP 7) ---------------------------------------------------

LOOKASIDE = "https://lookaside.fbsbx.com/ig_messaging/attach-123.jpg"


@pytest.fixture()
def submitted(monkeypatch):
    """The Pending batches handed to the dispatcher, instead of running turns."""
    captured: list = []
    monkeypatch.setattr(adapter.dispatcher, "submit", lambda key, item: captured.append((key, item)))
    return captured


@pytest.fixture()
def downloads(monkeypatch):
    """Record attachment fetches; hand back a plausible inbound path."""
    calls: list[tuple[str, str]] = []

    def fake_download(self, url, destination_dir, *, default_extension=".jpg"):
        calls.append((url, default_extension))
        return f"data/inbound/fetched{default_extension}"

    monkeypatch.setattr(adapter.InstagramClient, "download_attachment", fake_download)
    return calls


def post_message(client, message_extra: dict, *, mid="aWdfYXR0YWNobWVudA==", text="", sender=IGSID):
    body = webhook_body(text, mid=mid, sender=sender, message_extra=message_extra)
    return post(client, body)


def test_an_image_attachment_reaches_the_batch_as_a_photo(
    client, configured, sent, submitted, downloads
):
    assert post_message(client, {"attachments": [{"type": "image", "payload": {"url": LOOKASIDE}}]}).status_code == 200
    key, pending = submitted[0]
    assert key == IGSID
    assert pending.image_paths == ["data/inbound/fetched.jpg"]
    assert downloads == [(LOOKASIDE, ".jpg")]


def test_a_forwarded_post_is_a_photo_the_customer_is_asking_about(
    client, configured, sent, submitted, downloads
):
    """`share` is the highest-value Instagram case: "بكام ده؟" attached to one
    of the shop's own reels."""
    assert post_message(
        client,
        {
            "text": "بكام ده؟",
            "attachments": [{"type": "share", "payload": {"url": LOOKASIDE}}],
        },
        mid="aWdfc2hhcmU=",
    ).status_code == 200
    _, pending = submitted[0]
    assert pending.image_paths == ["data/inbound/fetched.jpg"]
    assert "بكام ده؟" in pending.texts


def test_a_story_mention_downloads_and_carries_its_marker(
    client, configured, sent, submitted, downloads
):
    assert post_message(
        client,
        {"attachments": [{"type": "story_mention", "payload": {"url": LOOKASIDE}}]},
        mid="aWdfbWVudGlvbg==",
    ).status_code == 200
    _, pending = submitted[0]
    assert pending.image_paths == ["data/inbound/fetched.jpg"]
    assert any(adapter.STORY_MENTION_MARKER in t for t in pending.texts)


def test_a_voice_note_arrives_as_mp4_not_ogg(client, configured, sent, submitted, downloads):
    assert post_message(
        client,
        {"attachments": [{"type": "audio", "payload": {"url": LOOKASIDE}}]},
        mid="aWdfYXVkaW8=",
    ).status_code == 200
    _, pending = submitted[0]
    assert pending.audio_paths == ["data/inbound/fetched.mp4"]
    assert downloads == [(LOOKASIDE, ".mp4")]


def test_the_provider_chain_accepts_instagram_audio_mime():
    """Instagram voice notes are audio/mp4. The chain has to accept that shape
    end to end: file extension -> mime -> the provider's wire format."""
    import tempfile
    from pathlib import Path

    from chatbot.media import _read
    from chatbot.providers.openrouter import _AUDIO_FORMATS

    assert _AUDIO_FORMATS.get("audio/mp4") == "mp4"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "voice.mp4"
        path.write_bytes(b"\x00\x01audio-bytes")
        payload = _read(str(path))
    assert payload is not None
    audio, mime = payload
    assert mime == "audio/mp4"


def test_an_unsupported_attachment_hands_off_and_acknowledges(
    client, configured, sent, seeded
):
    assert post_message(
        client,
        {"attachments": [{"type": "video", "payload": {"url": LOOKASIDE}}]},
        mid="aWdfdmlkZW8=",
    ).status_code == 200

    seeded.expire_all()
    items = queues.open_items(seeded, QueueKind.HANDOFF.value)
    assert len(items) == 1
    assert items[0].reason == "out_of_scope"
    assert items[0].payload["attachment_type"] == "video"
    assert len(sent) == 1
    assert "الفريق" in sent[0]["message"]["text"]


def test_a_failed_download_still_leaves_a_chaseable_path(
    client, configured, submitted, monkeypatch
):
    monkeypatch.setattr(
        adapter.InstagramClient, "download_attachment", lambda self, url, *a, **k: None
    )
    assert post_message(
        client,
        {"attachments": [{"type": "image", "payload": {"url": LOOKASIDE}}]},
        mid="aWdmYWlsZQ==",
    ).status_code == 200
    _, pending = submitted[0]
    assert pending.image_paths == [f"instagram-media:{LOOKASIDE}"]


def test_a_story_reply_keeps_its_context(client, configured, sent, submitted):
    assert post_message(
        client,
        {
            "text": "دي جميلة",
            "reply_to": {"story": {"id": "story-9", "url": "https://example.com/story.jpg"}},
        },
        mid="aWdzdG9yeQ==",
    ).status_code == 200
    _, pending = submitted[0]
    assert pending.extras["story_id"] == "story-9"
    assert any(adapter.STORY_REPLY_MARKER in t for t in pending.texts)


def test_a_reply_to_another_message_is_annotated_through_the_shared_helper(
    client, configured, sent, submitted
):
    assert post_message(
        client,
        {"text": "مقاس M لو سمحت", "reply_to": {"mid": "aWdfcGljdHVyZTE="}},
        mid="aWdyZXBseQ==",
    ).status_code == 200
    _, pending = submitted[0]
    assert pending.reply_to == {"aWdyZXBseQ==": "aWdfcGljdHVyZTE="}


def test_a_reply_to_an_earlier_photo_in_the_same_batch_gets_the_photo_label(
    configured, downloads
):
    """The fix: image attachments must land in `pending.image_ids`, not just
    `pending.image_paths` -- `Pending.annotated_text()` builds its "replying
    to photo N" label map by walking `image_ids`/`audio_ids`, so a photo that
    never registers an id can never be labelled, no matter what `reply_to`
    says. Regression test for a real gap: the id-less lists let `reply_to`
    populate correctly while the label silently never resolved."""
    from chatbot.dispatcher import Pending

    client = adapter.InstagramClient()
    pending = Pending()

    photo_mid = "aWdfcGhvdG8x"
    photo_message = {
        "mid": photo_mid,
        "attachments": [{"type": "image", "payload": {"url": LOOKASIDE}}],
    }
    assert adapter._collect_message(photo_message, photo_mid, pending, client, IGSID)

    reply_mid = "aWdfcmVwbHkx"
    reply_message = {"text": "مقاس M لو سمحت", "reply_to": {"mid": photo_mid}}
    assert adapter._collect_message(reply_message, reply_mid, pending, client, IGSID)

    assert pending.image_ids == [photo_mid]
    assert pending.reply_to == {reply_mid: photo_mid}
    assert "[replying to photo 1] مقاس M لو سمحت" in pending.annotated_text()


def test_a_reply_to_an_earlier_voice_note_in_the_same_batch_gets_the_voice_label(
    configured, downloads
):
    """Same fix, the audio side: a voice note's mid has to land in
    `pending.audio_ids` for a later reply to it to resolve to "voice note N"."""
    from chatbot.dispatcher import Pending

    client = adapter.InstagramClient()
    pending = Pending()

    voice_mid = "aWdfdm9pY2Ux"
    voice_message = {
        "mid": voice_mid,
        "attachments": [{"type": "audio", "payload": {"url": LOOKASIDE}}],
    }
    assert adapter._collect_message(voice_message, voice_mid, pending, client, IGSID)

    reply_mid = "aWdfcmVwbHky"
    reply_message = {"text": "تمام كده", "reply_to": {"mid": voice_mid}}
    assert adapter._collect_message(reply_message, reply_mid, pending, client, IGSID)

    assert pending.audio_ids == [voice_mid]
    assert pending.reply_to == {reply_mid: voice_mid}
    assert "[replying to voice note 1] تمام كده" in pending.annotated_text()


def test_a_crashed_turn_releases_the_claim_for_an_image_only_message(
    client, configured, sent, downloads, monkeypatch
):
    """The other half of the same gap: `_deliver`'s exception handler releases
    claims for `pending.text_ids + pending.image_ids + pending.audio_ids`. A
    photo-only message (no text at all) had nothing in any of those lists
    before the fix, so a crash mid-turn left its claim forever -- the message,
    and every Meta retry of it, silently swallowed. With `image_ids`
    populated, the claim comes back and the same mid can be reprocessed."""

    def boom(*args, **kwargs):
        raise RuntimeError("the turn blew up before answering")

    monkeypatch.setattr(adapter, "handle_message", boom)

    mid = "aWdfY3Jhc2hlZA=="
    assert post_message(
        client,
        {"attachments": [{"type": "image", "payload": {"url": LOOKASIDE}}]},
        mid=mid,
    ).status_code == 200

    with SessionLocal() as db:
        claimed = db.query(WebhookEvent).filter(
            WebhookEvent.platform_message_id == f"ig:{mid}"
        ).first()
    assert claimed is None  # released, not stuck forever


# --- tapped quick replies (STEP 8) ------------------------------------------


def test_a_tapped_quick_reply_arrives_formatted(client, configured, submitted):
    """The tap arrives with the title as `text` and the stored value as
    `quick_reply.payload`; formatted the same way WhatsApp formats an
    interactive tap."""
    assert post_message(
        client,
        {"text": "القاهرة", "quick_reply": {"payload": "Cairo"}},
        mid="aWdxcg==",
    ).status_code == 200
    _, pending = submitted[0]
    assert pending.texts == ["القاهرة (Cairo)"]
