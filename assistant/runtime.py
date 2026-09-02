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
    #: What each attached photo is of, keyed by path -- see
    #: `assistant/agent.py::AgentReply.attachment_labels`. The adapter pairs
    #: it with the platform id each photo went out as, which is what lets a
    #: customer's reply to one of four colour photos resolve to one colour.
    attachment_labels: dict[str, str] = field(default_factory=dict)
    #: An interactive payload the adapter should send instead of plain text
    #: (a governorate list, a size picker). None on every channel that has no
    #: such thing, which is why it never reaches the model.
    interactive: dict | None = None
    #: True when the conversation is with a human and the bot stayed silent.
    paused: bool = False
    #: True when this message had already been processed (a platform retry).
    duplicate: bool = False
    #: True when the turn produced no text on purpose: the customer was
    #: already answered by the tool that ran (the order confirmation). Not
    #: silence to warn about -- see `assistant/agent.py::AgentReply.silent`.
    silent: bool = False
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


def record_inbound(
    channel: str,
    external_id: str,
    text: str,
    *,
    images: list[str] | None = None,
    audio: list[str] | None = None,
    message_id: str | None = None,
) -> bool:
    """Store a customer's message the moment it arrives, and commit it.

    The dashboard reads `sessions`, and until this existed a row was only
    written by `agent.run_turn` -- *after* the model had answered. Everything
    between arrival and a successful reply was therefore invisible: a
    conversation the bot was still thinking about, one paused for a staff
    member who could not see it to release it, one whose turn crashed and
    rolled the whole transaction back, and one the model simply never replied
    to all looked exactly like a customer who had never written. That is how
    three numbers went unanswered without anyone noticing.

    So the record is written here instead, on the ingest path, in its **own**
    committed transaction: it must survive whatever the turn does next,
    including a rollback. Costs one short `UPDATE` in the webhook request,
    which is the same price `claim_message` already pays and buys the one
    thing the shop cannot reconstruct afterwards.

    Never raises: a failure to record must not be a failure to answer.
    """
    if not (text or "").strip() and not images and not audio:
        return False
    try:
        with session_scope() as db:
            session_store.append(
                db,
                channel,
                external_id,
                msg.user(
                    text or "",
                    images=images,
                    audio=audio,
                    provisional=message_id or None,
                ),
            )
    except Exception:
        log.exception(
            "could not record the inbound message from %s/%s on arrival; the turn "
            "still runs, but the dashboard will not show it until the bot replies",
            channel,
            external_id,
        )
        return False
    return True


def record_outbound(
    channel: str,
    external_id: str,
    text: str,
    *,
    db: Session | None = None,
    delivered: bool = True,
    message_ids: list[str] | None = None,
) -> None:
    """Store a message the shop started, so the dashboard shows it too.

    Registered onto `domain/services/notifications.py` at startup (see
    `register_transcript_recorder` there) and called by every send that does
    not come out of an agent turn: the order confirmation, the Shopify
    status pushes, the delivery feedback request, the back-in-stock notice
    and the abandoned-cart nudge. All of them reach the customer's phone; none
    of them used to reach `sessions`, so the dashboard showed a strictly
    shorter conversation than the one that actually happened, and a staff
    member reading it could not see what the shop had already told the
    customer.

    Writes through `db` when the caller has a transaction open, and opens its
    own when it does not. Both matter. Joining the caller's transaction is
    what makes the transcript line and the row that marks the message handled
    (`StockWaitlistEntry.notified_at`, `AbandonedCartNudge.sent_at`) commit or
    roll back together, instead of a second connection queueing behind the
    first for a write lock the first will not release until it is done. And
    the order confirmation has no transaction to join -- it is sent *after*
    its order commits, on purpose -- so it gets one of its own.

    Recorded as an assistant message with `by="system"`, which
    `assistant/display.py` and the dashboard carry through: an automated push
    is neither the model's own words nor a staff member's, and a transcript
    that cannot tell those apart is how someone ends up answering for a
    sentence nobody chose.

    `delivered=False` says the customer never got this one: outside Meta's
    24-hour customer service window with no approved template, or refused
    outright. Called with the same text as a line already stored, it marks
    *that* line rather than writing a second copy -- the failure is usually
    only learned about after the message was recorded, because the record is
    written inside the transaction that decided it and the send waits for the
    commit. A transcript that showed those as delivered is what let a shipped
    order's tracking update go missing with nobody the wiser.

    `message_ids` are the ids the platform gave this message once it was
    actually sent, and they arrive on a *second* call for a line already
    stored -- the send happens after the transaction that recorded it. They
    are what a later read receipt is matched against
    (`session.record_receipt`), so without them an order confirmation or a
    shipping update could never be shown as seen. Attaching them is best
    effort: a message with no ids simply has no receipt to show.

    Never raises -- see the caller's `_record`.
    """
    if not (text or "").strip():
        return
    if db is not None:
        _write_outbound(db, channel, external_id, text, delivered, message_ids)
        return
    with session_scope() as own:
        _write_outbound(own, channel, external_id, text, delivered, message_ids)


def _write_outbound(
    db: Session,
    channel: str,
    external_id: str,
    text: str,
    delivered: bool,
    message_ids: list[str] | None = None,
) -> None:
    if message_ids:
        # An annotation on a line that already exists, never a new one. If the
        # line has gone (an archive cap, a purge) there is nothing to stamp
        # and nothing to say -- the message was still delivered.
        session_store.attach_ids_to_text(db, channel, external_id, text, message_ids)
        return
    if not delivered and session_store.mark_undelivered(db, channel, external_id, text):
        return
    session_store.append(
        db, channel, external_id, msg.assistant(text, by="system", delivered=delivered)
    )


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
    recorded_ids: set[str] | None = None,
    reply_to: list[str] | None = None,
    mids: list[str] | None = None,
) -> RuntimeReply:
    """`recorded_ids` are the platform message ids already stored on arrival by
    `record_inbound`. Their provisional copies are folded into the real message
    this turn writes, so the transcript does not say everything twice.

    `mids` are those same ids kept *on* the stored message, so a later
    "reply to this" can find it; `reply_to` is the other direction -- the ids
    of the earlier messages this one is quoting, resolved against the
    transcript in `assistant/quoting.py`."""
    if db is not None:
        return _handle(
            db,
            channel,
            external_id,
            text,
            image_paths,
            audio_paths,
            platform_message_id,
            provider,
            recorded_ids,
            reply_to,
            mids,
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
            recorded_ids,
            reply_to,
            mids,
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
    recorded_ids: set[str] | None = None,
    reply_to: list[str] | None = None,
    mids: list[str] | None = None,
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
        session_store.append(
            db,
            channel,
            external_id,
            msg.user(_stored_text(text, image_paths, audio_paths), mids=mids),
            recorded_ids=recorded_ids,
        )
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
                msg.user(
                    _stored_text(text, image_paths, audio_paths),
                    audio=list(audio_paths),
                    mids=mids,
                ),
                recorded_ids=recorded_ids,
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
                msg.user(
                    _stored_text(text, image_paths, audio_paths),
                    images=list(image_paths),
                    mids=mids,
                ),
                recorded_ids=recorded_ids,
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
        # Nothing to answer: an empty body, a reaction, a message whose only
        # content was media that could not be read. Named rather than dropped
        # in silence -- from the customer's side this is the bot ignoring
        # them, and it used to leave no trace anywhere that it had happened.
        log.warning(
            "no answerable content in the message from %s/%s; no reply will be sent",
            channel,
            external_id,
        )
        return RuntimeReply()

    # One inbound message reads the shelf once, however many catalog tools the
    # model decides to call while composing the reply. Opened here rather than
    # inside the tools so the snapshot cannot outlive the message: the next one
    # asks Shopify again.
    with shopify_catalog.turn_scope():
        reply = agent.run_turn(
            db,
            channel,
            external_id,
            text,
            provider=provider,
            images=image_paths,
            audio=audio_paths,
            recorded_ids=recorded_ids,
            reply_to=reply_to,
            mids=mids,
        )
    return RuntimeReply(
        text=reply.text,
        attachments=reply.attachments,
        attachment_labels=reply.attachment_labels,
        interactive=reply.interactive,
        tool_calls=reply.tool_calls,
        error=reply.error,
        silent=reply.silent,
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
