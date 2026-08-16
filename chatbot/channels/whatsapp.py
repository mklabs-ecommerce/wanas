"""The WhatsApp channel adapter -- inbound.

The only WhatsApp-specific thing in the conversational path. It parses Meta's
webhook, calls the same `handle_message(channel, external_id, text)` the local
harness calls, and delivers the reply. There is no WhatsApp-specific
conversational logic beyond the platform integration itself.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Request, Response

from backend.config import PROJECT_ROOT, settings
from backend.db import session_scope
from backend.integrations.whatsapp_client import WhatsAppClient
from backend.services import notifications
from chatbot.runtime import handle_message
from chatbot.tools.support_tools import raise_handoff

log = logging.getLogger("wanas.channel.whatsapp")

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

CHANNEL = "whatsapp"
INBOUND_MEDIA_DIR = PROJECT_ROOT / "data" / "inbound"

#: Anything that is not text and not a photo. Phase 1 cannot act on a voice
#: note or a location, and guessing at one is worse than handing it to a person.
UNSUPPORTED_TYPES = {"audio", "video", "document", "sticker", "location", "contacts"}


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Confirms the request genuinely came from Meta, not "any request that
    showed up"."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@router.get("")
def verify(request: Request) -> Response:
    """Meta's one-time subscription handshake."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == settings.whatsapp_verify_token:
        if not settings.whatsapp_verify_token:
            return Response("verify token not configured", status_code=503)
        return Response(params.get("hub.challenge", ""), media_type="text/plain")
    return Response("forbidden", status_code=403)


@router.post("")
async def inbound(request: Request) -> Response:
    if not settings.whatsapp_configured:
        # Inert until Meta credentials exist, rather than half-working.
        return Response("whatsapp not configured", status_code=503)

    raw = await request.body()
    if settings.whatsapp_app_secret and not verify_signature(
        settings.whatsapp_app_secret, raw, request.headers.get("x-hub-signature-256")
    ):
        log.warning("rejected a webhook with a bad signature")
        return Response("bad signature", status_code=403)

    try:
        payload = await request.json()
    except Exception:
        return Response("ok", status_code=200)

    for message, contact_name in _iter_messages(payload):
        try:
            _process(message, contact_name)
        except Exception:  # never let one bad message stop the batch
            log.exception("failed to process inbound message %s", message.get("id"))

    # Always 200: Meta retries anything else, and the idempotency table is
    # what makes a retry safe rather than a duplicate order.
    return Response("ok", status_code=200)


def _iter_messages(payload: dict):
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name")
                for contact in value.get("contacts") or []
            }
            for message in value.get("messages") or []:
                yield message, names.get(message.get("from"))


def _process(message: dict, contact_name: str | None) -> None:
    external_id = message.get("from")
    message_id = message.get("id")
    message_type = message.get("type")
    if not external_id:
        return

    client = WhatsAppClient()
    text = ""
    image_paths: list[str] | None = None

    if message_type == "text":
        text = (message.get("text") or {}).get("body", "")
    elif message_type == "image":
        image = message.get("image") or {}
        text = image.get("caption", "") or ""
        downloaded = client.download_media(image.get("id", ""), INBOUND_MEDIA_DIR)
        # Even if the download fails the photo still has to reach a person --
        # the media id is enough for staff to chase it.
        image_paths = [downloaded or f"whatsapp-media:{image.get('id')}"]
    elif message_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        text = reply.get("title", "")
    elif message_type in UNSUPPORTED_TYPES:
        with session_scope() as session:
            raise_handoff(
                session,
                CHANNEL,
                external_id,
                "out_of_scope",
                f"Customer sent a {message_type} message, which the bot cannot handle in Phase 1",
                payload={"message_type": message_type, "platform_message_id": message_id},
            )
        client.send_text(external_id, "وصلتني رسالتك، حد من الفريق هيرد عليك حالاً 🙏")
        return
    else:
        log.info("ignoring unsupported whatsapp message type %r", message_type)
        return

    reply = handle_message(
        CHANNEL,
        external_id,
        text,
        image_paths=image_paths,
        platform_message_id=message_id,
    )

    if reply.duplicate or not reply.text:
        return

    client.send_text(external_id, reply.text)
    for path in reply.attachments:
        # The model wrote the words; the adapter decides how the picture is
        # delivered. Text carries the answer, the image supports it.
        client.send_image(external_id, path)


def register_outbound_sender() -> bool:
    """Called at startup. Until this runs, the Notification service's default
    LogSender keeps everything else working."""
    if not settings.whatsapp_configured:
        log.warning("WhatsApp credentials not set: outbound messages will be logged, not sent")
        return False
    notifications.register_sender(WhatsAppClient())
    log.info("WhatsApp outbound sender registered")
    return True
