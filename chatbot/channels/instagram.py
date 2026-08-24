"""The Instagram channel adapter -- inbound.

Same architecture as the WhatsApp adapter (`chatbot/channels/whatsapp.py`),
which is the template this mirrors deliberately: parse Meta's webhook, claim
the message id, download anything short-lived, answer 200 -- and run the agent
turn later, on a worker thread, through `chatbot/dispatcher.py`. There is no
Instagram-specific conversational logic beyond the platform integration
itself; everything downstream is the same `handle_message(channel,
external_id, text)` the harness calls.

What actually differs from WhatsApp is the envelope, not the flow. Instagram
DMs arrive Messenger-shaped (`entry[].messaging[]`, `sender.id`,
`message.mid`) rather than WhatsApp-shaped, ids are opaque IGSIDs rather than
phone numbers, and the platform signals echoes and deletions with flags on
the message instead of separate event types. Two defensive rules matter more
here than they ever did on WhatsApp and are pinned by their own tests:

* **Never answer your own messages.** A subscribed echo -- or anything whose
  `sender.id` equals the shop's own account id -- is dropped before the
  idempotency claim, or the bot answers itself forever.
* **Every claim id is prefixed** (`ig:` for DMs, `igc:` for comments later).
  `WebhookEvent.platform_message_id` is one shared primary key across every
  channel; an unprefixed Instagram `mid` colliding with a WhatsApp id would
  silently swallow a real customer's message.
"""

from __future__ import annotations

import logging
import re
import zlib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response

from backend.config import PROJECT_ROOT, settings
from backend.db import session_scope
from backend.integrations.instagram_client import InstagramClient
from backend.models import Channel, QueueKind, utcnow
from backend.security import verify_signature
from backend.services import identities, notifications, queues
from chatbot import messages as msg
from chatbot import session as session_store
from chatbot.dispatcher import MessageDispatcher, Pending
from chatbot.runtime import claim_message, handle_message, release_claims
from chatbot.tools.support_tools import raise_handoff

log = logging.getLogger("wanas.channel.instagram")

router = APIRouter(prefix="/webhooks/instagram", tags=["instagram"])

#: The plan's constant, verbatim. Everything -- sessions, carts, identities,
#: the dashboard -- keys off this string; a second spelling anywhere would
#: split one person into two conversations.
CHANNEL = Channel.INSTAGRAM_DM.value

INBOUND_MEDIA_DIR = PROJECT_ROOT / "data" / "inbound"

UNSUPPORTED_ACK = "وصلتني رسالتك، حد من الفريق هيرد عليك حالاً 🙏"


@router.get("")
def verify(request: Request) -> Response:
    """Meta's one-time subscription handshake."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.instagram_verify_token
    ):
        if not settings.instagram_verify_token:
            return Response("verify token not configured", status_code=503)
        return Response(params.get("hub.challenge", ""), media_type="text/plain")
    return Response("forbidden", status_code=403)


@router.post("")
async def inbound(request: Request) -> Response:
    if not settings.instagram_configured:
        # Inert until Meta credentials exist, rather than half-working.
        return Response("instagram not configured", status_code=503)

    raw = await request.body()
    # Signed with the *Instagram* app secret -- a different string from
    # WHATSAPP_APP_SECRET even inside the same Meta app.
    if settings.instagram_app_secret and not verify_signature(
        settings.instagram_app_secret, raw, request.headers.get("x-hub-signature-256")
    ):
        log.warning("rejected a webhook with a bad signature")
        return Response("bad signature", status_code=403)

    try:
        payload = await request.json()
    except Exception:
        return Response("ok", status_code=200)

    for kind, item, entry_time in _route(payload):
        try:
            if kind == "message":
                _accept_message(item)
            else:
                _accept_comment(item, entry_time)
        except Exception:  # never let one bad item stop the batch
            # Whatever was claimed inside has to be given back, or Meta's
            # retry -- and every retry after it -- is suppressed forever.
            _release_item_claims(item)
            log.exception("failed to accept an inbound instagram %s", kind)

    # Always 200: Meta retries anything else, and the idempotency claim taken
    # in `_accept_*` is what makes a retry safe rather than a duplicate reply.
    return Response("ok", status_code=200)


def _route(payload: dict):
    """Both surfaces this webhook serves, flattened in arrival order.

    `entry[].messaging[]` are DM events; `entry[].changes[]` with
    `field == "comments"` are comment events.
    """
    for entry in payload.get("entry") or []:
        entry_time = entry.get("time")
        for messaging in entry.get("messaging") or []:
            yield "message", messaging, None
        for change in entry.get("changes") or []:
            if change.get("field") == "comments":
                yield "comment", change.get("value") or {}, entry_time


def _item_claim_ids(item: dict) -> list[str]:
    """The prefixed claim id(s) one routed item may hold."""
    if "message" in item or "mid" in item:
        message = item.get("message") or item
        mid = message.get("mid")
        return [f"ig:{mid}"] if mid else []
    comment_id = item.get("id")
    return [f"igc:{comment_id}"] if comment_id else []


def _release_item_claims(item: dict) -> None:
    ids = _item_claim_ids(item)
    if ids:
        release_claims(ids)


# --------------------------------------------------------------------------
# ingest -- fast, in the request
# --------------------------------------------------------------------------


def _is_own_account(sender_id: str | None) -> bool:
    """Whether this event came from the shop's own account.

    Answering your own outbound messages -- because `message_echoes` got
    subscribed, or because Meta delivered the bot's own send back as an
    event -- is the bot talking to itself forever, publicly. It is the single
    worst failure available on this channel, so it is checked before the
    claim, before everything.
    """
    return bool(sender_id) and sender_id == settings.instagram_account_id


def _accept_message(messaging: dict) -> None:
    """Everything that has to happen before the webhook answers.

    Attachment downloads live here rather than on the worker (STEP 7) for the
    same reason the WhatsApp adapter downloads in-request: Instagram's
    attachment URLs are short-lived and the debounce window is not.
    """
    sender_id = (messaging.get("sender") or {}).get("id")
    message = messaging.get("message") or {}
    if not sender_id or not message:
        return

    # The bot never hears itself. Checked before the claim: an echo must not
    # consume the idempotency slot either.
    if _is_own_account(sender_id):
        log.info("dropping a message from the shop's own account (%s)", sender_id)
        return
    if message.get("is_echo") or message.get("is_deleted") or message.get("is_unsupported"):
        log.info(
            "dropping an instagram message event (echo=%s deleted=%s unsupported=%s)",
            bool(message.get("is_echo")),
            bool(message.get("is_deleted")),
            bool(message.get("is_unsupported")),
        )
        return

    mid = message.get("mid") or ""
    if not claim_message(_claim_id(mid)):
        log.info("ignoring duplicate delivery %s", mid)
        return

    client = InstagramClient()
    # Seen + typing now, because the answer is seconds away -- same reasoning
    # as WhatsApp's blue ticks.
    client.mark_seen(sender_id)
    client.typing_on(sender_id)

    pending = Pending(last_message_id=mid)
    if not _collect_message(message, mid, pending, client, sender_id):
        # An unsupported attachment took the handoff path and acknowledged it;
        # nothing goes to the agent.
        return

    if not (pending.texts or pending.image_paths or pending.audio_paths):
        log.info("nothing actionable in instagram message %s", mid)
        return

    dispatcher.submit(sender_id, pending)


def _claim_id(mid: str) -> str:
    """The idempotency key. Prefixed, always: `WebhookEvent.platform_message_id`
    is shared across every channel, and comments use `igc:`."""
    return f"ig:{mid}"


def _chase_path(url: str) -> str:
    """What is stored in place of a path when a download failed, so staff can
    still chase the attachment -- matching the WhatsApp fallback's shape."""
    return f"instagram-media:{url[:80]}"


#: Attachment types the bot cannot act on. Handed to a person with a visible
#: ack, exactly like WhatsApp's list -- guessing at one is worse.
UNSUPPORTED_ATTACHMENT_TYPES = {
    "video",
    "file",
    "location",
    "template",
    "like_heart",
    "fallback",
}

STORY_REPLY_MARKER = "[رد على ستوري]"
STORY_MENTION_MARKER = "[الزبون منشنك في ستوري]"


def _collect_message(
    message: dict, mid: str, pending: Pending, client: InstagramClient, sender_id: str
) -> bool:
    """Pull one message's content into the batch.

    Returns False when the message carried an attachment type the bot cannot
    act on; that path has already handed off to a person and acknowledged,
    mirroring how WhatsApp's adapter treats its unsupported types.
    """
    text = message.get("text") or ""

    # A long-pressed reply to an earlier message, a reply to a live story, or
    # a story @mention all arrive as `reply_to` / `story_mention`. Treating
    # them as ordinary messages loses real context ("ده" means *the thing in
    # the story*), so each becomes words in the turn instead.
    reply_to = message.get("reply_to") or {}
    if isinstance(reply_to, dict):
        story = reply_to.get("story")
        if story:
            text = f"{STORY_REPLY_MARKER} {text}".strip()
            pending.extras["story_id"] = story.get("id")
        elif reply_to.get("mid"):
            pending.reply_to[mid] = reply_to["mid"]

    # A tapped quick reply arrives with the title as `text` and the stored
    # value alongside it. Formatted the same way WhatsApp's adapter formats
    # an interactive tap: the id is what shipping lookup wants, the title is
    # what makes the transcript readable.
    quick_reply = message.get("quick_reply") or {}
    if isinstance(quick_reply, dict) and quick_reply.get("payload"):
        picked = str(quick_reply["payload"])
        title = text.strip()
        text = f"{title} ({picked})" if title else picked

    for attachment in message.get("attachments") or []:
        att_type = attachment.get("type")
        payload = attachment.get("payload") or {}
        url = payload.get("url") or ""

        if att_type == "story_mention":
            downloaded = client.download_attachment(url, INBOUND_MEDIA_DIR)
            pending.image_paths.append(downloaded or _chase_path(url))
            pending.image_ids.append(mid)
            text = f"{STORY_MENTION_MARKER} {text}".strip()
            continue

        if att_type == "audio":
            # Instagram voice notes are audio/mp4, not ogg like WhatsApp's --
            # the default extension is what makes the file name one the
            # transcriber accepts (chatbot/media.py::_AUDIO_MIME).
            downloaded = client.download_attachment(
                url, INBOUND_MEDIA_DIR, default_extension=".mp4"
            )
            pending.audio_paths.append(downloaded or _chase_path(url))
            pending.audio_ids.append(mid)
            continue

        if att_type in {"image", "share"}:
            # `share` is a post/reel the customer forwarded -- "بكام ده؟"
            # attached to one of the shop's own reels. It downloads as a
            # picture; the vision pass + matched_product resolves it to a
            # product like any other photo.
            downloaded = client.download_attachment(url, INBOUND_MEDIA_DIR)
            pending.image_paths.append(downloaded or _chase_path(url))
            pending.image_ids.append(mid)
            continue

        if att_type in UNSUPPORTED_ATTACHMENT_TYPES:
            with session_scope() as session:
                raise_handoff(
                    session,
                    CHANNEL,
                    sender_id,
                    "out_of_scope",
                    f"Customer sent an instagram {att_type} attachment, which the bot cannot handle",
                    payload={"attachment_type": att_type, "platform_message_id": mid},
                )
            client.send_text(sender_id, UNSUPPORTED_ACK)
            return False

        log.info("ignoring unsupported instagram attachment type %r", att_type)

    if text.strip():
        pending.texts.append(text)
        pending.text_ids.append(mid or None)
    return True


# --------------------------------------------------------------------------
# comments -- a public surface, shipped OFF (INSTAGRAM_COMMENTS_ENABLED=0)
# --------------------------------------------------------------------------

#: Fixed, short, human lines -- NOT a model call. Rotated deterministically
#: so the account's comments do not read as a bot loop. A comment is answered
#: publicly in a few words and continued privately in DM, where the real
#: conversation happens.
PUBLIC_ACKS = (
    "بعتلك رسالة في الدايركت 🖤",
    "جوابك في الدايركت ✨",
    "شوف الدايركت، بعتلك كل التفاصيل 🖤",
)

#: Emoji ranges, variation selectors and zero-width joiners -- what gets
#: stripped before deciding whether a comment actually said anything.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # pictographs, emoji
    "\U00002600-\U000027BF"  # misc symbols
    "\U0001F1E6-\U0001F1FF"  # flags
    "\U00002B00-\U00002BFF"
    "\u200d\ufe0f"           # joiners / selectors
    "❤✨🖤"
    "]+"
)


def _comment_says_nothing(text: str) -> bool:
    """True for an empty text, emoji-only, or under 3 non-emoji characters.
    "🔥🔥" is not a question and does not deserve a DM."""
    stripped = _EMOJI_RE.sub("", text or "")
    stripped = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    return len(stripped) < 3


def _aware(value: datetime) -> datetime:
    """SQLite hands naive datetimes back out of DateTime(timezone=True)
    columns; PostgreSQL returns aware ones. Normalise instead of assuming."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse_comment_timestamp(raw) -> datetime | None:
    """Meta sends ISO strings on comments; entry times are epoch seconds."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _accept_comment(value: dict, entry_time=None) -> None:
    """Comment ingest: a strict filter chain, then exactly two actions.

    The chain runs in the order the plan fixes, each drop an INFO log naming
    the reason. What survives gets one fixed public ack (never a model call)
    and one private reply that opens the DM thread with the session already
    seeded, so the customer's next DM lands in a conversation that knows why
    they are here.

    The agent never runs on comment text: a full turn on "بكام؟" with no
    product context answers worse than an opener inviting them to say it.
    """
    from backend.models import InstagramCommentReply

    comment_id = str(value.get("id") or "")
    commenter = (value.get("from") or {}).get("id")
    text = value.get("text") or ""
    media_id = (value.get("media") or {}).get("id")

    # 1. The whole surface ships off; nothing below it may run.
    if not settings.instagram_comments_enabled:
        log.info("comments disabled: dropping comment %s", comment_id)
        return

    # 2. THE SHOP'S OWN COMMENT OR ITS OWN REPLY. Without this the bot replies
    #    to itself, forever, publicly, on a live post -- the worst available
    #    failure on this surface.
    if commenter == settings.instagram_account_id:
        log.info("dropping the shop's own comment/reply (%s)", comment_id)
        return

    # 3. A reply inside someone else's thread. Let DM carry it.
    if value.get("parent_id"):
        log.info("dropping threaded reply %s", comment_id)
        return

    # 4. Duplicate delivery. Prefixed like every claim: `igc:` shares the
    #    webhook_events table with `ig:` and every WhatsApp id.
    if not claim_message(f"igc:{comment_id}"):
        log.info("ignoring duplicate comment delivery %s", comment_id)
        return

    # 5. Too old. Meta's own private-reply window is 7 days; past the
    #    configured max age a reply is noise, not service.
    timestamp = _parse_comment_timestamp(value.get("timestamp")) or _parse_comment_timestamp(
        entry_time
    )
    if timestamp is not None:
        age_hours = (utcnow() - timestamp).total_seconds() / 3600
        if age_hours > settings.instagram_comment_max_age_hours:
            log.info(
                "dropping comment %s: %.0fh old exceeds the %.0fh max",
                comment_id,
                age_hours,
                settings.instagram_comment_max_age_hours,
            )
            return

    # 6. Per-commenter cap inside a rolling hour. One person spamming a post
    #    must cost staff's attention once, not forty model-free sends.
    with session_scope() as db:
        # Counted in Python with normalised datetimes rather than compared in
        # SQL: SQLite stores these columns naive and PostgreSQL aware, and a
        # bound aware parameter does not lexically compare against either.
        cutoff = utcnow() - timedelta(hours=1)
        rows = (
            db.query(InstagramCommentReply)
            .filter(InstagramCommentReply.commenter_igsid == commenter)
            .all()
        )
        recent = sum(
            1
            for row in rows
            if row.created_at is not None
            and _aware(row.created_at) >= cutoff
        )
        if recent >= settings.instagram_comment_rate_limit:
            log.warning(
                "dropping comment %s: %s is over the comment rate limit (%d/hour)",
                comment_id,
                commenter,
                settings.instagram_comment_rate_limit,
            )
            queues.enqueue(
                db,
                kind=QueueKind.ALERT.value,
                reason="comment_flood",
                summary=f"Comment flood from {commenter} on media {media_id}; "
                "comments are being dropped",
                channel=CHANNEL,
                external_id=commenter,
                payload={"comment_id": comment_id, "media_id": media_id},
            )
            return

        # A row existing means this comment was handled once already --
        # checked here as well as via the claim, because the row is what
        # survives a crash between write and send.
        if db.get(InstagramCommentReply, comment_id) is not None:
            log.info("blocking re-handled comment %s: a reply row exists", comment_id)
            return

        # Written BEFORE anything is sent: a crash after this point leaves a
        # row that stops the retry, never a second DM to someone who got one.
        db.add(
            InstagramCommentReply(
                comment_id=comment_id,
                media_id=media_id,
                commenter_igsid=commenter,
                public_replied=False,
                private_replied=False,
            )
        )
        db.flush()

    # 7. Empty, emoji-only, under three real characters.
    if _comment_says_nothing(text):
        log.info("dropping comment %s: no actionable text", comment_id)
        return

    client = InstagramClient()

    # a) The public ack -- deterministic per comment id (crc32; Python's hash()
    #    is salted per process and would pick differently on a retry).
    if settings.instagram_public_reply_enabled:
        ack = PUBLIC_ACKS[zlib.crc32(comment_id.encode("utf-8")) % len(PUBLIC_ACKS)]
        result = client.reply_to_comment(comment_id, ack)
        if result.delivered:
            _mark_comment(comment_id, public_replied=True)

    # b) The private reply -- what actually starts the conversation.
    opener = f"شفت كومنتك على البوست 👀\n«{text[:200]}»\nقولّي وأنا تحت أمرك 🖤"
    private = client.send_private_reply(comment_id, opener)
    if not private.delivered:
        # The row above keeps any retry from double-DMing; staff can chase
        # this through the log line.
        log.error("private reply to comment %s failed: %s", comment_id, private.error)
        return

    _mark_comment(comment_id, private_replied=True)
    igsid = private.to
    if not igsid:
        log.error("private reply to %s returned no recipient id; session not seeded", comment_id)
        return

    # Seed the thread so the customer's next message lands mid-conversation,
    # with what they commented on already in it.
    with session_scope() as db:
        identities.get_or_create(db, CHANNEL, igsid)
        session_store.append(
            db,
            CHANNEL,
            igsid,
            msg.user(f"[كومنت على بوست {media_id}] {text}"),
            msg.assistant(opener),
        )


def _mark_comment(comment_id: str, **flags: bool) -> None:
    from backend.models import InstagramCommentReply

    with session_scope() as db:
        row = db.get(InstagramCommentReply, comment_id)
        if row is not None:
            for field, value in flags.items():
                setattr(row, field, value)


# --------------------------------------------------------------------------
# delivery -- on a worker thread, after the debounce window
# --------------------------------------------------------------------------


def _deliver(external_id: str, pending: Pending) -> None:
    try:
        reply = handle_message(
            CHANNEL,
            external_id,
            pending.annotated_text(),
            image_paths=pending.image_paths or None,
            audio_paths=pending.audio_paths or None,
        )
    except Exception:
        # The turn died before the message was handled, and every id in the
        # batch was claimed at ingest. Release them so Meta's retry is
        # processed instead of suppressed forever.
        claimed = [i for i in pending.text_ids + pending.image_ids + pending.audio_ids if i]
        release_claims([_claim_id(i) for i in claimed])
        raise

    if reply.duplicate or not (reply.text or reply.interactive):
        return

    client = InstagramClient()

    with session_scope() as db:
        interactive_enabled = runtime_flags_enabled(db)

    outcomes = []
    if reply.interactive and interactive_enabled:
        # The picker carries its own prompt, so the model's words go first --
        # two messages, in the order a person would send them. On Instagram
        # the "picker" degrades to quick replies or numbered text inside the
        # client; the adapter does not care which.
        if reply.text:
            outcomes.append(client.send_text(external_id, reply.text))
        outcomes.append(
            client.send_interactive(external_id, reply.interactive, fallback=reply.text or "")
        )
    elif reply.text:
        outcomes.append(client.send_text(external_id, reply.text))

    for path in reply.attachments:
        outcomes.append(client.send_image(external_id, path))

    _flag_delivery_failures(external_id, outcomes)


def runtime_flags_enabled(db) -> bool:
    from backend.services import runtime_flags

    return runtime_flags.get(db, "interactive_messages_enabled", settings.interactive_messages_enabled)


def _flag_delivery_failures(external_id: str, outcomes: list) -> None:
    """The model composed a reply and Meta refused every attempt to send it.

    Same pattern and same justification as the WhatsApp adapter's: a log line
    nobody watches reads as the bot ignoring the customer. Its own alert
    reason, so staff can tell an Instagram delivery problem from a WhatsApp
    one at a glance.
    """
    failed = [o for o in outcomes if not o.delivered]
    if not failed:
        return
    log.error(
        "outbound instagram delivery failed for %s: %s",
        external_id,
        "; ".join(o.error or "unknown error" for o in failed),
    )
    with session_scope() as db:
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason="instagram_reply_delivery_failed",
            summary=f"Instagram reply to {external_id} failed to send",
            channel=CHANNEL,
            external_id=external_id,
            payload={"errors": [o.error for o in failed]},
        )


#: One per process, built at import time so the router and the tests share it.
dispatcher = MessageDispatcher(_deliver)


def register_outbound_sender() -> bool:
    """Called at startup. Until this runs, the Notification service's default
    LogSender keeps everything else working -- and an unregistered Instagram
    channel can never borrow WhatsApp's client."""
    if not settings.instagram_configured:
        log.warning("Instagram credentials not set: outbound messages will be logged, not sent")
        return False
    notifications.register_sender(InstagramClient(), channel=CHANNEL)
    log.info("Instagram outbound sender registered")
    return True
