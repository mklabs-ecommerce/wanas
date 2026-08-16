"""The single entry point every channel calls.

    handle_message(channel, external_id, text)

The WhatsApp adapter, the local harness, and any future channel all come
through here. Everything channel-independent lives in this file: idempotency,
the pause flag, the image rule, and the agent turn.

Swapping the harness for WhatsApp changes the adapter and nothing else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db import session_scope
from backend.models import WebhookEvent
from backend.services import identities, shopify_catalog
from chatbot import agent
from chatbot import messages as msg
from chatbot import session as session_store
from chatbot.providers import LLMProvider
from chatbot.tools.support_tools import raise_handoff

log = logging.getLogger("wanas.runtime")

#: What the customer is told when their photo goes to a person. The model
#: never sees the image, so it cannot be the thing that describes it.
IMAGE_ACK = "وصلتني الصورة 📸 حد من الفريق هيبصلها ويرد عليك حالاً."


@dataclass
class RuntimeReply:
    text: str | None = None
    attachments: list[str] = field(default_factory=list)
    #: True when the conversation is with a human and the bot stayed silent.
    paused: bool = False
    #: True when this message had already been processed (a platform retry).
    duplicate: bool = False
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None


def _already_processed(db: Session, platform_message_id: str | None) -> bool:
    """Check before processing, insert as part of processing.

    Platforms retry delivery when they do not get a fast enough response.
    Without this a retry creates a duplicate order or double-decrements stock.
    """
    if not platform_message_id:
        return False
    if db.get(WebhookEvent, platform_message_id) is not None:
        return True
    db.add(WebhookEvent(platform_message_id=platform_message_id))
    try:
        db.flush()
    except IntegrityError:
        # Two deliveries of the same message racing each other.
        db.rollback()
        return True
    return False


def handle_message(
    channel: str,
    external_id: str,
    text: str = "",
    *,
    image_paths: list[str] | None = None,
    platform_message_id: str | None = None,
    db: Session | None = None,
    provider: LLMProvider | None = None,
) -> RuntimeReply:
    if db is not None:
        return _handle(db, channel, external_id, text, image_paths, platform_message_id, provider)
    with session_scope() as session:
        return _handle(session, channel, external_id, text, image_paths, platform_message_id, provider)


def _handle(
    db: Session,
    channel: str,
    external_id: str,
    text: str,
    image_paths: list[str] | None,
    platform_message_id: str | None,
    provider: LLMProvider | None,
) -> RuntimeReply:
    if _already_processed(db, platform_message_id):
        log.info("ignoring duplicate delivery %s", platform_message_id)
        return RuntimeReply(duplicate=True)

    identity = identities.get_or_create(db, channel, external_id)
    identities.detect_pending_link_from_external_id(db, identity)

    # A paused conversation is being handled by a person. The message is
    # stored so staff have the full thread, and the model is not called at
    # all -- only a staff action in the dashboard clears the flag.
    if identity.paused_until_staff_reply:
        session_store.append(db, channel, external_id, msg.user(_stored_text(text, image_paths)))
        log.info("conversation %s/%s is paused; message stored, not answered", channel, external_id)
        return RuntimeReply(paused=True)

    if image_paths:
        # Raised by the runtime, before the model sees anything. In Phase 1
        # the model is never handed an image, so it cannot classify one -- a
        # guess about a garment the shop may not make is worse than a handoff.
        session_store.append(db, channel, external_id, msg.user(_stored_text(text, image_paths)))
        raise_handoff(
            db,
            channel,
            external_id,
            "image_received",
            (text.strip() or "Customer sent a photo"),
            payload={"images": list(image_paths)},
        )
        return RuntimeReply(text=IMAGE_ACK, paused=True)

    if not (text or "").strip():
        return RuntimeReply()

    # One inbound message reads the shelf once, however many catalog tools the
    # model decides to call while composing the reply. Opened here rather than
    # inside the tools so the snapshot cannot outlive the message: the next one
    # asks Shopify again.
    with shopify_catalog.turn_scope():
        reply = agent.run_turn(db, channel, external_id, text, provider=provider)
    return RuntimeReply(
        text=reply.text,
        attachments=reply.attachments,
        tool_calls=reply.tool_calls,
        error=reply.error,
    )


def _stored_text(text: str, image_paths: list[str] | None) -> str:
    if image_paths:
        tag = f"[صورة: {', '.join(image_paths)}]"
        return f"{text.strip()} {tag}".strip()
    return text


def staff_reply(db: Session, channel: str, external_id: str, text: str) -> None:
    """A staff member answering as the shop from the handoff queue.

    Recorded in the same history the model reads, so when the conversation is
    handed back the bot knows what was already said.
    """
    session_store.append(db, channel, external_id, msg.assistant(text))
