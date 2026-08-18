"""The single entry point every channel calls.

    handle_message(channel, external_id, text)

The WhatsApp adapter, the local harness, and any future channel all come
through here. Everything channel-independent lives in this file: idempotency,
the pause flag, the media rules, and the agent turn.

Swapping the harness for WhatsApp changes the adapter and nothing else.

The media rules are the part that moved. A voice note and a photo used to be
handled by the *adapter*, which meant WhatsApp knew something about
conversational policy that no other channel could share. They are decided
here now, from `chatbot/media.py`, so the harness exercises the same paths the
real channel does -- and so "what happens to a photo" is one answer rather than
one per channel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db import session_scope
from backend.models import WebhookEvent
from backend.services import identities, shopify_catalog
from chatbot import agent, media
from chatbot import messages as msg
from chatbot import session as session_store
from chatbot.providers import LLMProvider, get_provider
from chatbot.tools.support_tools import raise_handoff

log = logging.getLogger("wanas.runtime")

#: What the customer is told when their photo goes to a person, because
#: nothing could be read from it. The model never saw the image in this case,
#: so it cannot be the thing that describes it.
IMAGE_ACK = "وصلتني الصورة 📸 حد من الفريق هيبصلها ويرد عليك حالاً."

#: Same, for a voice note the provider could not transcribe.
VOICE_ACK = "وصلتني رسالتك الصوتية 🎧 حد من الفريق هيسمعها ويرد عليك حالاً."


@dataclass
class RuntimeReply:
    text: str | None = None
    attachments: list[str] = field(default_factory=list)
    #: An interactive payload the adapter should send instead of plain text
    #: (a governorate list, a size picker). None on every channel that has no
    #: such thing, which is why it never reaches the model.
    interactive: dict | None = None
    #: True when the conversation is with a human and the bot stayed silent.
    paused: bool = False
    #: True when this message had already been processed (a platform retry).
    duplicate: bool = False
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    #: What the customer actually said, after a voice note was transcribed.
    #: The adapter logs it; nothing branches on it.
    transcript: str | None = None


def claim_message(platform_message_id: str | None, *, db: Session | None = None) -> bool:
    """Reserve a platform message id, in its own committed transaction.

    Called at *ingest*, before the work is queued, which is the only place it
    can do its job now that the reply is produced after the webhook has already
    answered 200. Meta retries a delivery it thinks failed; without a committed
    claim, a retry that arrives while the first copy is still being answered
    creates a second order.

    True means "this is ours to process", False means someone already has it.
    """
    if not platform_message_id:
        return True
    if db is not None:
        return not _already_processed(db, platform_message_id)
    with session_scope() as session:
        return not _already_processed(session, platform_message_id)


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
    audio_paths: list[str] | None = None,
    platform_message_id: str | None = None,
    db: Session | None = None,
    provider: LLMProvider | None = None,
) -> RuntimeReply:
    if db is not None:
        return _handle(
            db, channel, external_id, text, image_paths, audio_paths, platform_message_id, provider
        )
    with session_scope() as session:
        return _handle(
            session,
            channel,
            external_id,
            text,
            image_paths,
            audio_paths,
            platform_message_id,
            provider,
        )


def _handle(
    db: Session,
    channel: str,
    external_id: str,
    text: str,
    image_paths: list[str] | None,
    audio_paths: list[str] | None,
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
    # all -- only a staff action clears the flag.
    if identity.paused_until_staff_reply:
        session_store.append(db, channel, external_id, msg.user(_stored_text(text, image_paths, audio_paths)))
        log.info("conversation %s/%s is paused; message stored, not answered", channel, external_id)
        return RuntimeReply(paused=True)

    # Resolved once, and used for the media passes as well as the turn, so a
    # transcription and the reply it produces cannot come from two different
    # models.
    provider = provider or get_provider()
    transcript: str | None = None

    if audio_paths:
        transcript = media.transcribe_voice(provider, audio_paths[0], hint=text)
        if not transcript:
            # Unreadable, or no provider that can listen. The same fallback
            # this path has always had.
            session_store.append(
                db, channel, external_id, msg.user(_stored_text(text, image_paths, audio_paths))
            )
            raise_handoff(
                db,
                channel,
                external_id,
                "voice_received",
                (text.strip() or "Customer sent a voice note the bot could not transcribe"),
                payload={"audio": list(audio_paths)},
            )
            return RuntimeReply(text=VOICE_ACK, paused=True)

        log.info("transcribed a voice note for %s/%s (%s chars)", channel, external_id, len(transcript))
        # From here it is an ordinary text message. The tag stays on the
        # stored copy so staff reading the thread can tell it was spoken.
        text = f"{text.strip()} {transcript}".strip() if text.strip() else transcript

    if image_paths:
        reading = media.read_photo(db, provider, image_paths[0])
        if reading is None:
            # Nothing could be read -- unconfigured, unsupported, or the file
            # never arrived. A guess about a garment the shop may not make is
            # worse than a handoff, which is what this path has always done.
            session_store.append(
                db, channel, external_id, msg.user(_stored_text(text, image_paths, audio_paths))
            )
            raise_handoff(
                db,
                channel,
                external_id,
                "image_received",
                (text.strip() or "Customer sent a photo"),
                payload={"images": list(image_paths)},
            )
            return RuntimeReply(text=IMAGE_ACK, paused=True)

        product = media.matched_product(db, reading)
        log.info(
            "read a photo for %s/%s: match=%s confidence=%.2f garment=%s",
            channel,
            external_id,
            product.product_id if product else None,
            reading.confidence,
            reading.is_garment,
        )
        text = media.photo_context(reading, product, caption=text)

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
        interactive=reply.interactive,
        tool_calls=reply.tool_calls,
        error=reply.error,
        transcript=transcript,
    )


def _stored_text(text: str, image_paths: list[str] | None, audio_paths: list[str] | None = None) -> str:
    tags = []
    if image_paths:
        tags.append(f"[صورة: {', '.join(image_paths)}]")
    if audio_paths:
        tags.append(f"[رسالة صوتية: {', '.join(audio_paths)}]")
    return " ".join([text.strip(), *tags]).strip()


def staff_reply(db: Session, channel: str, external_id: str, text: str) -> None:
    """A staff member answering as the shop from the handoff queue.

    Recorded in the same history the model reads, so when the conversation is
    handed back the bot knows what was already said.
    """
    session_store.append(db, channel, external_id, msg.assistant(text))
