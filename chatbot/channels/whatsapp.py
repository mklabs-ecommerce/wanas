"""The WhatsApp channel adapter -- inbound.

The only WhatsApp-specific thing in the conversational path. It parses Meta's
webhook, calls the same `handle_message(channel, external_id, text)` the local
harness calls, and delivers the reply. There is no WhatsApp-specific
conversational logic beyond the platform integration itself.

What the endpoint does **not** do any more is the work. Parsing, the signature
check and the idempotency claim happen in the request; the agent turn happens
on a worker thread, after the debounce window, through `chatbot/dispatcher.py`.
The reason is in that module's docstring, and the consequence is here: this
handler returns 200 in milliseconds instead of holding the event loop for the
length of a model call.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Request, Response

from backend.config import PROJECT_ROOT, settings
from backend.db import session_scope
from backend.integrations.whatsapp_client import WhatsAppClient
from backend.services import notifications, runtime_flags
from chatbot.dispatcher import MessageDispatcher, Pending
from chatbot.runtime import claim_message, handle_message
from chatbot.tools.support_tools import raise_handoff

log = logging.getLogger("wanas.channel.whatsapp")

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])

CHANNEL = "whatsapp"
INBOUND_MEDIA_DIR = PROJECT_ROOT / "data" / "inbound"

#: Anything that is not text, a photo or a voice note. Phase 1 cannot act on a
#: location or a document, and guessing at one is worse than handing it to a
#: person. Audio is deliberately **not** here any more -- it is transcribed.
UNSUPPORTED_TYPES = {"video", "document", "sticker", "location", "contacts"}

#: Voice notes and forwarded audio arrive under different type names.
AUDIO_TYPES = {"audio", "voice", "ptt"}

UNSUPPORTED_ACK = "وصلتني رسالتك، حد من الفريق هيرد عليك حالاً 🙏"


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
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.whatsapp_verify_token
    ):
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
            _accept(message, contact_name)
        except Exception:  # never let one bad message stop the batch
            log.exception("failed to accept inbound message %s", message.get("id"))

    # Always 200: Meta retries anything else, and the idempotency claim taken
    # in `_accept` is what makes a retry safe rather than a duplicate order.
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


# --------------------------------------------------------------------------
# ingest -- fast, in the request
# --------------------------------------------------------------------------


def _accept(message: dict, contact_name: str | None) -> None:
    """Everything that has to happen before the webhook answers.

    Media download lives here rather than on the worker because Meta's media
    URLs are short-lived and the debounce window is not: a voice note fetched
    six seconds later is a fetch that can fail for a reason the customer will
    never understand.
    """
    external_id = message.get("from")
    message_id = message.get("id")
    message_type = message.get("type")
    if not external_id:
        return

    # The claim is committed on its own, before any work is queued. A Meta
    # retry that lands while the first copy is still being answered has to see
    # it -- otherwise the customer gets two replies, or two orders.
    if not claim_message(message_id):
        log.info("ignoring duplicate delivery %s", message_id)
        return

    client = WhatsAppClient()
    # Blue ticks and a typing bubble now, because the answer is seconds away
    # and an unread message is what makes someone send it again.
    client.mark_as_read(message_id)

    pending = Pending(last_message_id=message_id)

    if message_type == "text":
        pending.texts.append((message.get("text") or {}).get("body", "") or "")
    elif message_type == "image":
        image = message.get("image") or {}
        caption = image.get("caption", "") or ""
        if caption:
            pending.texts.append(caption)
        downloaded = client.download_media(image.get("id", ""), INBOUND_MEDIA_DIR)
        # Even if the download fails the photo still has to reach a person --
        # the media id is enough for staff to chase it.
        pending.image_paths.append(downloaded or f"whatsapp-media:{image.get('id')}")
    elif message_type in AUDIO_TYPES:
        audio = message.get(message_type) or message.get("audio") or {}
        downloaded = client.download_media(
            audio.get("id", ""), INBOUND_MEDIA_DIR, default_extension=".ogg"
        )
        pending.audio_paths.append(downloaded or f"whatsapp-media:{audio.get('id')}")
    elif message_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        # The row id is the stored value (`Cairo`), the title is what the
        # customer saw (`القاهرة`). Both are sent: the id is what the shipping
        # lookup wants, and the title is what makes the transcript readable.
        picked = reply.get("id") or ""
        title = reply.get("title", "") or ""
        pending.texts.append(f"{title} ({picked})".strip() if picked and title else (title or picked))
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
        client.send_text(external_id, UNSUPPORTED_ACK)
        return
    else:
        log.info("ignoring unsupported whatsapp message type %r", message_type)
        return

    if contact_name:
        pending.extras["contact_name"] = contact_name

    dispatcher.submit(external_id, pending)


# --------------------------------------------------------------------------
# delivery -- on a worker thread, after the debounce window
# --------------------------------------------------------------------------


def _deliver(external_id: str, pending: Pending) -> None:
    reply = handle_message(
        CHANNEL,
        external_id,
        pending.text,
        image_paths=pending.image_paths or None,
        audio_paths=pending.audio_paths or None,
    )

    if reply.duplicate or not (reply.text or reply.interactive):
        return

    client = WhatsAppClient()

    with session_scope() as db:
        interactive_enabled = runtime_flags.get(
            db, "interactive_messages_enabled", settings.interactive_messages_enabled
        )

    if reply.interactive and interactive_enabled:
        # The picker carries its own prompt, so the model's words go first and
        # the tappable list follows -- two messages, in the order a person
        # would send them.
        if reply.text:
            client.send_text(external_id, reply.text)
        client.send_interactive(external_id, reply.interactive, fallback=reply.text or "")
    elif reply.text:
        client.send_text(external_id, reply.text)

    for path in reply.attachments:
        # The model wrote the words; the adapter decides how the picture is
        # delivered. Text carries the answer, the image supports it.
        client.send_image(external_id, path)


#: One per process. Built at import time so the router and the tests share it.
dispatcher = MessageDispatcher(_deliver)


def register_outbound_sender() -> bool:
    """Called at startup. Until this runs, the Notification service's default
    LogSender keeps everything else working."""
    if not settings.whatsapp_configured:
        log.warning("WhatsApp credentials not set: outbound messages will be logged, not sent")
        return False
    notifications.register_sender(WhatsAppClient())
    log.info("WhatsApp outbound sender registered")
    return True
