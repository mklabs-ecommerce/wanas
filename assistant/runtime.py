"""The single entry point every channel calls.

    handle_message(channel, external_id, text)

The WhatsApp adapter, the local harness, and any future channel all come
through here. Everything channel-independent lives in this file: idempotency,
the pause flag, the media rules, and the agent turn.

Swapping the harness for WhatsApp changes the adapter and nothing else.

The media rules are the part that moved. A voice note and a photo used to be
handled by the *adapter*, which meant WhatsApp knew something about
conversational policy that no other channel could share. They are decided
here now, from `assistant/media.py`, so the harness exercises the same paths the
real channel does -- and so "what happens to a photo" is one answer rather than
one per channel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from assistant import agent, media, messages as msg, session as session_store
from assistant.providers import LLMProvider, get_provider
from assistant.tools.support_tools import raise_handoff
from common.timeutil import as_aware
from domain.db import session_scope
from domain.models import QueueKind, StaffQueueItem, WebhookEvent, utcnow
from domain.services import identities
from integrations.shopify import catalog as shopify_catalog

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


def release_claims(platform_message_ids: list[str] | str | None) -> int:
    """Give back message ids claimed by a delivery that was never handled.

    The claim exists so a platform retry cannot double-process a message while
    the first copy is still being answered. The price used to be that a copy
    which *crashed* mid-turn kept its claim forever, so every retry of it was
    then suppressed by design -- silence with no way back in. Deleting the row
    restores the retry; the success path never calls this, so a handled
    message still cannot be processed twice.

    Runs in its own committed transaction, because by the time this is called
    the failed unit of work has already rolled back or been abandoned.
    """
    if isinstance(platform_message_ids, str):
        platform_message_ids = [platform_message_ids]
    ids = [i for i in (platform_message_ids or []) if i]
    if not ids:
        return 0
    with session_scope() as session:
        deleted = session.execute(
            delete(WebhookEvent).where(WebhookEvent.platform_message_id.in_(ids))
        ).rowcount
    if deleted:
        log.warning(
            "released %d webhook claim(s) after a failed turn (%s); platform retries will be processed",
            deleted,
            ", ".join(ids),
        )
    return deleted


def _paused_note(db: Session, channel: str, external_id: str) -> str:
    """How long this conversation has been waiting on a person, as far as the
    data can say. There is no paused-at column; the newest handoff item for
    the identity is the best available marker. Its absence means a manual
    takeover from the dashboard -- say so rather than guess."""
    item = db.scalar(
        select(StaffQueueItem)
        .where(
            StaffQueueItem.channel == channel,
            StaffQueueItem.external_id == external_id,
            StaffQueueItem.kind == QueueKind.HANDOFF.value,
        )
        .order_by(StaffQueueItem.created_at.desc())
    )
    if item is None or item.created_at is None:
        return "no handoff record (manual takeover?)"
    hours = (utcnow() - as_aware(item.created_at)).total_seconds() / 3600
    return (
        f"handoff {item.queue_id} ({item.status}, reason={item.reason}) created "
        f"{item.created_at.isoformat()}, {hours:.1f}h ago"
    )


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
    # all -- only a staff action clears the flag. This drop used to be
    # completely silent, which is how a latched pause looked like the bot
    # ignoring one number; make it observable without changing the semantics.
    if identity.paused_until_staff_reply:
        session_store.append(db, channel, external_id, msg.user(_stored_text(text, image_paths, audio_paths)))
        log.warning(
            "dropping inbound message: conversation %s/%s is paused for staff reply (%s); "
            "message stored, no reply will be sent until a staff member releases it",
            channel,
            external_id,
            _paused_note(db, channel, external_id),
        )
        return RuntimeReply(paused=True)

    # Resolved once, and used for the media passes as well as the turn, so a
    # transcription and the reply it produces cannot come from two different
    # models.
    provider = provider or get_provider()
    transcript: str | None = None

    if audio_paths:
        # Every voice note in the batch, not just the first -- a customer who
        # sends two in a row used to have the second one silently ignored.
        transcripts = []
        for path in audio_paths:
            piece = media.transcribe_voice(db, provider, path, hint=text)
            if piece:
                transcripts.append(piece)

        if not transcripts:
            # Unreadable, or no provider that can listen. The same fallback
            # this path has always had.
            session_store.append(
                db,
                channel,
                external_id,
                msg.user(_stored_text(text, image_paths, audio_paths), audio=list(audio_paths)),
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

        transcript = "\n".join(transcripts)
        log.info("transcribed a voice note for %s/%s (%s chars)", channel, external_id, len(transcript))
        # From here it is an ordinary text message. The tag stays on the
        # stored copy so staff reading the thread can tell it was spoken.
        text = f"{text.strip()} {transcript}".strip() if text.strip() else transcript

    if image_paths:
        # Every photo in the batch, not just the first -- a customer who
        # sends two photos used to have the second one never looked at by the
        # model at all, not even acknowledged.
        readings: list[tuple] = []
        for path in image_paths:
            reading = media.read_photo(db, provider, path)
            if reading is None:
                continue
            product = media.matched_product(db, reading)
            log.info(
                "read a photo for %s/%s: match=%s confidence=%.2f garment=%s",
                channel,
                external_id,
                product.product_id if product else None,
                reading.confidence,
                reading.is_garment,
            )
            readings.append((reading, product))

        if not readings:
            # Nothing could be read from any of them -- unconfigured,
            # unsupported, or the files never arrived. A guess about a
            # garment the shop may not make is worse than a handoff, which is
            # what this path has always done.
            session_store.append(
                db,
                channel,
                external_id,
                msg.user(_stored_text(text, image_paths, audio_paths), images=list(image_paths)),
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

        if len(readings) == 1:
            # Unchanged from before: the single-photo case reads exactly as
            # it always did, caption folded straight into the one note.
            reading, product = readings[0]
            text = media.photo_context(reading, product, caption=text)
        else:
            # Multiple photos: the customer's own words go once, up front --
            # they are a message about the batch, not about any one photo --
            # then each reading gets its own numbered note so "the second
            # one" in the customer's next message is something the model can
            # actually resolve against.
            parts = [text.strip()] if text.strip() else []
            for index, (reading, product) in enumerate(readings, start=1):
                parts.append(f"Photo {index}: " + media.photo_context(reading, product))
            text = "\n".join(parts)

    if not (text or "").strip():
        return RuntimeReply()

    # One inbound message reads the shelf once, however many catalog tools the
    # model decides to call while composing the reply. Opened here rather than
    # inside the tools so the snapshot cannot outlive the message: the next one
    # asks Shopify again.
    with shopify_catalog.turn_scope():
        reply = agent.run_turn(
            db, channel, external_id, text, provider=provider, images=image_paths, audio=audio_paths
        )
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
