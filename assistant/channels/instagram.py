"""The Instagram channel adapter -- inbound.

Same architecture as the WhatsApp adapter (`assistant/channels/whatsapp.py`),
which is the template this mirrors deliberately: parse Meta's webhook, claim
the message id, download anything short-lived, answer 200 -- and run the agent
turn later, on a worker thread, through `assistant/dispatcher.py`. There is no
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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response

from assistant import (
    comment_faq,
    comment_replies,
    messages as msg,
    session as session_store,
)
from assistant.agent import GENERIC_FAILURE
from assistant.dispatcher import MessageDispatcher, Pending
from assistant.providers import get_provider
from assistant.providers.base import (
    COMMENT_CATEGORIES,
    LEGACY_COMMENT_CATEGORIES,
    ProviderError,
)
from assistant.runtime import claim_message, handle_message, record_inbound, release_claims
from assistant.tools.support_tools import raise_handoff
from common.security import verify_signature
from common.timeutil import as_aware
from config.settings import PROJECT_ROOT, settings
from domain.db import session_scope
from domain.models import Channel, QueueKind, utcnow
from domain.services import (
    identities,
    notifications,
    queues,
)
from integrations.instagram.client import InstagramClient

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

    Checked against `settings.instagram_self_ids` rather than one id: Instagram
    Login hands the same account out as both a professional-account id and an
    app-scoped id, and comparing against only the first left the shop's own
    comments looking like a stranger's.
    """
    return bool(sender_id) and sender_id in settings.instagram_self_ids


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
        log.info("ignoring duplicate delivery %s from %s", mid, sender_id)
        return

    log.info("inbound instagram message from %s (%s)", sender_id, mid)

    client = InstagramClient()
    pending = Pending(last_message_id=mid)
    if not _collect_message(message, mid, pending, client, sender_id):
        # An unsupported attachment took the handoff path and acknowledged it;
        # nothing goes to the agent.
        return

    if not (pending.texts or pending.image_paths or pending.audio_paths):
        log.warning(
            "nothing actionable in instagram message %s from %s; no reply will be sent",
            mid,
            sender_id,
        )
        return

    # Same rule as WhatsApp: the transcript gets the message before the
    # debounce window, so a turn that stalls or crashes still leaves the
    # conversation visible to staff. See `runtime.record_inbound`.
    if record_inbound(
        CHANNEL,
        sender_id,
        pending.text,
        images=pending.image_paths or None,
        audio=pending.audio_paths or None,
        message_id=_claim_id(mid),
    ):
        pending.recorded_ids.add(_claim_id(mid))

    # Seen + typing after the record, never before -- same reasoning as
    # WhatsApp's blue ticks.
    client.mark_seen(sender_id)
    client.typing_on(sender_id)

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
            # transcriber accepts (assistant/media.py::_AUDIO_MIME).
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
            record_inbound(CHANNEL, sender_id, f"[{att_type}]", message_id=_claim_id(mid))
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

# The public wording moved to `assistant/comment_replies.py`. It used to be
# two tuples here -- PUBLIC_ACKS and POSITIVE_ACKS -- and one line per
# category everywhere else, which is what made two people asking the same
# question get byte-identical text back. Categories now own *banks*, and
# the rule that kept these here in the first place is unchanged: every
# public sentence is hand-written and fixed, never model-chosen.

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


def _post_context_note(media: dict | None) -> str:
    """A short, honest note about the post a comment or DM is tied to.

    Caption only, fetched fresh by the caller right before this is built --
    never cached, never a separate post -> product table (see
    `InstagramClient.get_media`). Framed the same way `assistant/media.py`
    frames a photo reading: a hint about which product the agent should
    check, never a claim about price or stock on its own.
    """
    if not media:
        return ""
    caption = (media.get("caption") or "").strip()
    if not caption:
        return ""
    return (
        f'سياق: البوست ده كابشنه "{caption[:300]}". لو الزبون بيسأل من غير ما يحدد منتج، '
        "ممكن يكون بيقصد اللي في البوست ده -- اتأكد بالأدوات قبل أي سعر أو توفر."
    )


#: What a classifier outage classifies as. Not a category the model can pick:
#: an outage must not look like a customer who said nothing, and it must not
#: produce a public reply or a DM either -- see `_classify`.
CLASSIFIER_UNAVAILABLE = "unavailable"


def _normalise_category(category: str) -> str:
    """A raw classifier answer, resolved to a category the routing knows.

    One place, on the way in, rather than at each lookup. Doing it per-lookup
    is how `_ACTIONS` fell back to `other` for a legacy `important` while the
    reply bank -- asked for `important` -- found nothing and published
    silence: the comment got its DM and no public line at all.
    """
    resolved = LEGACY_COMMENT_CATEGORIES.get(category, category)
    return resolved if resolved in COMMENT_CATEGORIES else "other"


def _classify(text: str, *, comment_id: str, media_id, commenter) -> str:
    """One of COMMENT_CATEGORIES, from the cheap classifier.

    Unavailable (no key, RehearsalProvider, a transient failure) falls back to
    CLASSIFIER_UNAVAILABLE -- nothing said, nothing sent -- and raises an
    alert. It used to fall back to "important", which meant a provider outage
    produced a *burst* of public replies and DMs on a live post with no model
    having decided any of them. Silence is the safe failure on a public
    surface; the `classifier_unavailable` alert is what keeps silence from
    meaning loss, since the owner can work the queue and answer those by hand.

    Note this is the one silence the redesign kept. Every *category* now
    answers somebody -- but an outage is not a category, and guessing at one
    would publish a line no model chose under a live post.
    """
    try:
        return _normalise_category(get_provider().classify_comment(text).category)
    except ProviderError as exc:
        log.warning("comment classification unavailable (%s); staying silent", exc.kind)
        _classifier_unavailable(comment_id, text, media_id, commenter, str(exc))
        return CLASSIFIER_UNAVAILABLE
    except Exception as exc:  # noqa: BLE001 -- the reason goes into the alert
        log.exception("comment classification failed; staying silent")
        _classifier_unavailable(comment_id, text, media_id, commenter, str(exc))
        return CLASSIFIER_UNAVAILABLE


def _classifier_unavailable(comment_id: str, text: str, media_id, commenter, error: str) -> None:
    """The comment nobody classified, handed to a person instead.

    Carries everything needed to answer it by hand without going back to
    Meta: what was said, which comment, which post, and who wrote it.
    """
    with session_scope() as db:
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason="classifier_unavailable",
            summary=f"Comment classifier unavailable ({error[:120]}); "
            f"unanswered comment from {commenter}: {text[:160]}",
            channel=CHANNEL,
            external_id=commenter,
            payload={
                "comment_id": comment_id,
                "media_id": media_id,
                "commenter_id": commenter,
                "text": text,
                "error": error[:300],
            },
        )


@dataclass(frozen=True)
class _Action:
    """What one comment category is allowed to do.

    A table rather than a branch, so "does every category actually answer
    somebody?" is a property you can *read* -- and assert in a test -- instead
    of a chain of ifs where a missing `else` is silence nobody notices. That
    is exactly how `negative` shipped: classified, alerted on, and never
    answered, for months.
    """

    #: Reply publicly, with a line picked from that category's bank.
    public: bool = True
    #: Open a DM. Spends the DM budget; see `_dm_budget_available`.
    dm: bool = False
    #: When the DM budget is gone, may the public line still go out alone?
    #: True only where silence is worse than a bare acknowledgement.
    public_reply_without_dm: bool = False
    #: Raise a queue item for staff, with this reason.
    alert_reason: str | None = None
    alert_label: str = ""
    alert_priority: str | None = None


#: Every category, and what it does. The five product questions route
#: identically -- public line plus DM -- and differ only in *wording*, which
#: is the whole point of splitting them: `comment_replies` has a separate bank
#: for each, so a size question and a price question no longer read as the
#: same sentence with a different noun.
_ACTIONS: dict[str, _Action] = {
    "price": _Action(dm=True),
    "availability": _Action(dm=True),
    "size": _Action(dm=True),
    "variant": _Action(dm=True),
    "product_info": _Action(dm=True),
    "order_status": _Action(
        dm=True,
        public_reply_without_dm=True,
        alert_reason="order_status_comment",
        alert_label="Order-status question",
    ),
    "complaint": _Action(
        dm=True,
        # A complaint keeps its public line even with the DM budget spent:
        # the alert is already raised and staff answer by hand, and silence
        # on a public complaint is the worst outcome available here.
        public_reply_without_dm=True,
        alert_reason="customer_complaint",
        alert_label="COMPLAINT",
        alert_priority="high",
    ),
    # Answered in public, and never DMed. A compliment is not a question, so
    # opening a DM for it would be the shop cold-messaging someone who only
    # said something nice.
    "positive": _Action(),
    "tag_friend": _Action(),
    # Answered publicly *and* flagged. The public line is for the hundred
    # people reading, not for the one who wrote it -- see `_NEGATIVE` in
    # `comment_replies`. No DM: chasing a critic into their inbox is how a
    # bad comment becomes a screenshot.
    "negative": _Action(
        alert_reason="negative_comment",
        alert_label="Negative comment",
    ),
    # The one category with no customer-visible action, deliberately. A public
    # answer to a scam bot republishes it to everyone reading the post, and a
    # DM walks into it. `hide_comment` still stays uncalled: hiding is
    # invisible to the shop, so a misclassified customer would vanish with no
    # trace. The owner hides by hand, from this alert.
    "spam": _Action(
        public=False,
        alert_reason="spam_comment",
        alert_label="Spam comment",
    ),
    "other": _Action(dm=True),
}


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


def _is_our_own_thread(parent_id: str) -> bool:
    """Whether this reply hangs under something *we* replied to.

    A `parent_id` used to be dropped unconditionally, which was right while
    the bot's only public output was "شوف الدايركت" -- nobody answers that.
    Now that a fixed FAQ answer goes out in public, a customer following up
    under it («طب والشحن بكام؟») is both likely and reasonable, so a reply in
    one of our own threads is processed like any other comment. A reply
    between two other people, in a thread that is none of the shop's
    business, is still dropped. The own-account check runs before this, so
    this cannot open a self-reply loop.
    """
    from domain.models import InstagramCommentReply

    with session_scope() as db:
        return db.get(InstagramCommentReply, parent_id) is not None


def _accept_comment(value: dict, entry_time=None) -> None:
    """Comment ingest: a strict filter chain, then one of four outcomes.

    The chain runs in the order the plan fixes, each drop an INFO log naming
    the reason. What survives is either answered in public from the fixed FAQ
    table and finished there, or handed to DM with one fixed public ack and a
    private reply that opens the thread with the session already seeded -- or
    it goes to the review queue and nowhere else.

    The agent never runs on comment text: a full turn on "بكام؟" with no
    product context answers worse than an opener inviting them to say it.
    """
    from domain.models import InstagramCommentReply

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
    #    failure on this surface. Stays first, ahead of the thread rule below.
    if commenter and commenter in settings.instagram_self_ids:
        log.info("dropping the shop's own comment/reply (%s)", comment_id)
        return

    # 3. A reply inside someone else's thread. Let DM carry it. A reply under
    #    one of *our* replies is a follow-up to something we said, and goes
    #    through the whole chain like any other comment -- see
    #    `_is_our_own_thread`.
    parent_id = value.get("parent_id")
    if parent_id and not _is_our_own_thread(str(parent_id)):
        log.info("dropping threaded reply %s", comment_id)
        return

    # 4. Duplicate delivery. Prefixed like every claim: `igc:` shares the
    #    webhook_events table with `ig:` and every WhatsApp id. Claimed even
    #    for a comment that says nothing, so a redelivery is not re-examined.
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

    # 6. Empty, emoji-only, under three real characters. Checked *before* the
    #    reply row is written: a "🔥" receives nothing, so it must not spend
    #    one of the commenter's hourly slots on the way to receiving it.
    if _comment_says_nothing(text):
        log.info("dropping comment %s: no actionable text", comment_id)
        return

    # 7. Which budget this comment spends, decided before it is counted.
    #    Matching is a pure lookup -- no model call, no send (see
    #    `assistant/comment_faq.py`) -- so asking here is free. With public
    #    replies switched off there is no public answer to give and the
    #    question falls through to the DM handoff: that flag means "do not
    #    speak in public", never "ignore the customer".
    faq_key = comment_faq.match(text) if settings.instagram_public_reply_enabled else None

    # 8. Per-commenter cap inside a rolling hour. One person spamming a post
    #    must cost staff's attention once, not forty model-free sends.
    #
    #    This is the *flood guard*, and it is deliberately not the DM budget.
    #    It used to be: every non-FAQ comment was counted against
    #    INSTAGRAM_COMMENT_RATE_LIMIT (3/hour) here, before anything knew
    #    whether a DM would be sent -- so three compliments in an hour spent
    #    the whole DM budget, and the real question that followed them was
    #    dropped without a reply. A comment that only ever gets a public line
    #    must not spend the cap that exists to stop a flood of *DMs*; the DM
    #    budget is checked where the DM is actually sent, at step 11.
    limit = settings.instagram_faq_rate_limit
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
            and as_aware(row.created_at) >= cutoff
            and bool(row.faq_key) == bool(faq_key)
        )
        # How many DMs this person has actually been sent in the same hour --
        # the budget step 11 spends, counted here while the rows are loaded.
        recent_dms = sum(
            1
            for row in rows
            if row.created_at is not None
            and as_aware(row.created_at) >= cutoff
            and row.private_replied
        )
        if recent >= limit:
            if faq_key:
                # Not a flood, and not an alert: nothing reached a person's
                # inbox and nothing cost a model call. Just a chatty commenter.
                log.info(
                    "dropping comment %s: %s is over the FAQ rate limit (%d/hour)",
                    comment_id,
                    commenter,
                    limit,
                )
                return
            log.warning(
                "dropping comment %s: %s is over the comment rate limit (%d/hour)",
                comment_id,
                commenter,
                limit,
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
        # row that stops the retry, never a second reply under someone's
        # comment or a second DM to someone who already got one.
        db.add(
            InstagramCommentReply(
                comment_id=comment_id,
                media_id=media_id,
                commenter_igsid=commenter,
                public_replied=False,
                private_replied=False,
                faq_key=faq_key,
            )
        )
        db.flush()

    client = InstagramClient()

    # 9. A question whose answer is fixed and identical for every customer.
    #    Answered in public, where it was asked and where everyone else
    #    scrolling past can read it -- and that is the whole interaction: no
    #    DM, no seeded session, and the classifier is never called, which is
    #    the saving.
    if faq_key:
        result = client.reply_to_comment(comment_id, comment_faq.reply_for(faq_key))
        if result.delivered:
            _mark_comment(comment_id, public_replied=True)
        else:
            log.error("public FAQ reply to comment %s failed: %s", comment_id, result.error)
        return

    # 10. Classify, then act. Every category below does *something* --
    #     the one thing this surface must never do is receive a real comment
    #     from a real person and answer it with nothing, which is what
    #     `negative` and the old `neither` did for months.
    #
    #     What a category is allowed to do is data, not a branch: see
    #     `_ACTIONS`. The words themselves live in
    #     `assistant/comment_replies.py`, one bank per category rather than
    #     one line, because two people asking the same question used to get
    #     byte-identical text back.
    category = _classify(text, comment_id=comment_id, media_id=media_id, commenter=commenter)
    action = _ACTIONS.get(category)
    if action is None:
        # Only CLASSIFIER_UNAVAILABLE reaches here -- `_normalise_category`
        # has already resolved every real answer. Its alert is raised inside
        # `_classify`, so there is nothing left to do but stay quiet.
        log.info("comment %s: classifier unavailable, nothing sent", comment_id)
        return

    # a) The internal half: staff see it, the customer does not.
    if action.alert_reason:
        _comment_alert(
            action.alert_reason,
            f"{action.alert_label} from {commenter} on media {media_id}: {text[:200]}",
            comment_id=comment_id,
            media_id=media_id,
            commenter=commenter,
            text=text,
            priority=action.alert_priority,
        )

    # b) The DM half, and the budget it spends. Checked before the public line
    #    is written, because a public "check your DMs" whose DM never comes is
    #    the broken promise this surface must never publish -- so when the
    #    budget is gone the handoff is downgraded to its public line alone
    #    (`complaint`, which must never be met with silence) or to nothing.
    wants_dm = action.dm and settings.instagram_comments_dm_enabled
    if wants_dm and not _dm_budget_available(
        recent_dms, comment_id=comment_id, commenter=commenter, media_id=media_id
    ):
        if not action.public_reply_without_dm:
            return
        wants_dm = False

    # c) The public half.
    public = comment_replies.public_reply(category, comment_id) if action.public else None
    if public is not None and not settings.instagram_public_reply_enabled:
        public = None

    if wants_dm:
        _dm_handoff(client, comment_id, text, media_id, category=category, public_reply=public)
        return

    if public is not None:
        result = client.reply_to_comment(comment_id, public)
        if result.delivered:
            _mark_comment(comment_id, public_replied=True)
        else:
            log.error("public reply to comment %s failed: %s", comment_id, result.error)
        return

    log.info("comment %s classified as %s: no customer-visible action", comment_id, category)


def _dm_budget_available(recent_dms: int, *, comment_id: str, commenter, media_id) -> bool:
    """Whether this commenter may still be DMed inside the rolling hour.

    Separate from the flood guard at step 8 on purpose: a compliment or an FAQ
    answer costs nobody an inbox, so neither may use up the three DMs an hour
    that exist to stop one person filling a real person's.
    """
    if recent_dms < settings.instagram_comment_rate_limit:
        return True
    log.warning(
        "not DMing comment %s: %s is over the DM rate limit (%d/hour)",
        comment_id,
        commenter,
        settings.instagram_comment_rate_limit,
    )
    with session_scope() as db:
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason="comment_flood",
            summary=f"Comment flood from {commenter} on media {media_id}; "
            "DMs are being withheld",
            channel=CHANNEL,
            external_id=commenter,
            payload={"comment_id": comment_id, "media_id": media_id},
        )
    return False


def _comment_alert(
    reason: str,
    summary: str,
    *,
    comment_id,
    media_id,
    commenter,
    text,
    priority: str | None = None,
) -> None:
    """One queue item about one comment.

    The only place a comment is escalated to a person, so every reason carries
    the same payload shape and staff read one thing, not four.
    """
    payload = {"comment_id": comment_id, "media_id": media_id, "text": text}
    if priority:
        payload["priority"] = priority
    with session_scope() as db:
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason=reason,
            summary=summary,
            channel=CHANNEL,
            external_id=commenter,
            payload=payload,
        )


def _dm_handoff(
    client: InstagramClient,
    comment_id: str,
    text: str,
    media_id,
    *,
    category: str,
    public_reply: str | None,
) -> None:
    """One public line, one private reply, one seeded session.

    Shared by every category that opens a DM. What differs between them is
    only *wording* -- which bank the public line and the opener are drawn
    from -- and neither is written by a model; see
    `assistant/comment_replies.py`.

    `public_reply` is None when the shop is not speaking in public at all
    (INSTAGRAM_PUBLIC_REPLY_ENABLED=0). That flag means "do not speak in
    public", never "ignore the customer", so the DM half still runs.
    """
    # a) The public line, when the shop is speaking in public at all.
    if public_reply is not None:
        result = client.reply_to_comment(comment_id, public_reply)
        if result.delivered:
            _mark_comment(comment_id, public_replied=True)
        else:
            log.error("public reply to comment %s failed: %s", comment_id, result.error)

    # b) The private reply -- what actually starts the conversation. Worded
    #    for the category, so a size question opens on sizes and a complaint
    #    opens on an apology, rather than all of them opening identically.
    opener = comment_replies.dm_opener(category, comment_id, text)
    private = client.send_private_reply(comment_id, opener)
    if not private.delivered:
        # The row written at ingest keeps any retry from double-DMing; staff
        # can chase this through the log line.
        log.error("private reply to comment %s failed: %s", comment_id, private.error)
        return

    _mark_comment(comment_id, private_replied=True)
    igsid = private.to
    if not igsid:
        log.error("private reply to %s returned no recipient id; session not seeded", comment_id)
        return

    # Seed the thread so the customer's next message lands mid-conversation,
    # with what they commented on already in it -- including the post's own
    # caption, fetched fresh right now (never cached; see
    # InstagramClient.get_media), so the agent has something to infer the
    # product from instead of asking cold.
    seed_text = f"[كومنت على بوست {media_id}] {text}"
    if media_id:
        note = _post_context_note(client.get_media(media_id))
        if note:
            seed_text = f"{seed_text}\n{note}"

    with session_scope() as db:
        identities.get_or_create(db, CHANNEL, igsid)
        session_store.append(
            db,
            CHANNEL,
            igsid,
            msg.user(seed_text),
            msg.assistant(opener),
        )


def _mark_comment(comment_id: str, **flags: bool) -> None:
    from domain.models import InstagramCommentReply

    with session_scope() as db:
        row = db.get(InstagramCommentReply, comment_id)
        if row is not None:
            for field, value in flags.items():
                setattr(row, field, value)


# --------------------------------------------------------------------------
# delivery -- on a worker thread, after the debounce window
# --------------------------------------------------------------------------


def _deliver(external_id: str, pending: Pending) -> None:
    text = pending.annotated_text()

    # A reply to a live story is tied to a specific post id (`story_id`,
    # captured in `_collect_message`) the same way a comment is tied to
    # `media_id` -- fetched fresh here, right before the turn runs, never
    # cached. Only fires when the customer actually replied to a story; an
    # ordinary DM has no story_id and this is a no-op.
    story_id = pending.extras.get("story_id")
    if story_id:
        note = _post_context_note(InstagramClient().get_media(story_id))
        if note:
            text = f"{text}\n{note}" if text else note

    try:
        reply = handle_message(
            CHANNEL,
            external_id,
            text,
            image_paths=pending.image_paths or None,
            audio_paths=pending.audio_paths or None,
            recorded_ids=pending.recorded_ids or None,
            # A quote pointing outside this debounce batch -- at something the
            # bot said, most often. Resolved against the stored transcript by
            # the runtime, exactly as on WhatsApp.
            reply_to=pending.unresolved_reply_to() or None,
            mids=[
                mid
                for mid in (*pending.text_ids, *pending.image_ids, *pending.audio_ids)
                if mid
            ]
            or None,
        )
    except Exception:
        # The turn died somewhere `agent.run_turn` doesn't already guard --
        # session/identity plumbing, media handling, the Shopify read -- and
        # every id in the batch was claimed at ingest. Release them so Meta's
        # retry is processed instead of suppressed forever.
        claimed = [i for i in pending.text_ids + pending.image_ids + pending.audio_ids if i]
        release_claims([_claim_id(i) for i in claimed])
        log.exception("turn crashed before producing a reply for %s; sending fallback", external_id)
        _send_crash_fallback(external_id)
        return

    if reply.duplicate:
        return
    if not (reply.text or reply.interactive):
        if reply.silent:
            # Deliberate: a tool already sent the customer this turn's
            # message (the order confirmation). Not dead air.
            log.info("turn for %s answered by a tool; nothing further to send", external_id)
            return
        log.warning(
            "turn for %s produced no reply to send (paused=%s error=%s)",
            external_id,
            reply.paused,
            reply.error,
        )
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
    _remember_sent_ids(external_id, outcomes)


def _remember_sent_ids(external_id: str, outcomes: list) -> None:
    """Stamp the ids Instagram gave this reply onto the message it stored, so
    a later "reply to this" on it resolves. See the WhatsApp adapter's twin."""
    sent = [mid for out in outcomes if out.delivered for mid in out.message_ids]
    if not sent:
        return
    try:
        with session_scope() as db:
            session_store.attach_outbound_ids(db, CHANNEL, external_id, sent)
    except Exception:
        log.exception("could not record the outbound message ids for %s", external_id)


def runtime_flags_enabled(db) -> bool:
    from domain.services import runtime_flags

    return runtime_flags.get(db, "interactive_messages_enabled", settings.interactive_messages_enabled)


def _send_crash_fallback(external_id: str) -> None:
    """Last line of defence: the turn never produced a reply at all.

    Same justification as the WhatsApp adapter's: the webhook already
    returned 200 at ingest, so nothing else will retry this. Sending is
    itself wrapped so a customer is never left silent because *this* also
    failed, and the alert queue tells staff a real bug happened.
    """
    try:
        InstagramClient().send_text(external_id, GENERIC_FAILURE)
    except Exception:
        log.exception("failed to send the crash fallback message to %s", external_id)
    with session_scope() as db:
        queues.enqueue(
            db,
            kind=QueueKind.ALERT.value,
            reason="turn_crashed",
            summary=f"Conversation with {external_id} hit an unhandled error; sent a generic apology",
            channel=CHANNEL,
            external_id=external_id,
        )


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
