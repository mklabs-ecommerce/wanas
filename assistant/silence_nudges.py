"""The "customer went silent" follow-up: at most two nudges per silence.

Replaces the abandoned-cart nudge, which fired once, two hours after the last
line was added to a cart, and only for carts. This covers every open
conversation the bot answered:

* **Nudge #1**, `NUDGE_FIRST_MINUTES` (10) after the customer's last message,
  on any conversation whose last word is the bot's. One short line in the
  brand voice, written by the chat model from the conversation itself
  ("لسه معانا؟ ..." naming what was being looked at), with a fixed sentence
  as the fallback when the model is unavailable or writes something that
  should not go out unreviewed (a number, a long paragraph, a non-Egyptian
  word).
* **Nudge #2**, `NUDGE_SECOND_HOURS` (2) after the same message, **only** with
  a cart still open -- the old abandoned-cart content, naming what is in it.
  A general chat gets no second nudge.

Then nothing, until the customer writes again: `SilenceNudge.anchor_at` is the
message both steps belong to, and a newer `ChannelIdentity.last_seen_at` is a
new anchor with both steps re-armed.

Never sent: outside Meta's 24-hour window (free-form only -- there is no
template here on purpose; a nudge is not worth a paid template), while the
conversation is paused for a person or was last answered by one, once an
order was placed in this silence, after the customer closed the conversation
("شكراً", "سلام", "مش عايزة") or asked us to stop (which is remembered past
this silence), and in Cairo quiet hours -- where #1 is dropped and #2 waits
for the end of them if the window is still open then.

Durable and single-shot across workers and instances: every decision lives on
the `silence_nudges` row, and a step is *claimed* with a conditional UPDATE
committed before anything is sent, so a redeploy resumes from the table and
two pollers racing the same identity send one message at most. A crash
between the claim and the send loses that nudge rather than repeating it --
the right side to fail on for an unsolicited message.

Lives in assistant/ because #1 needs the provider and the stored history;
`domain/services/scheduler.py` runs it through a registered job, the same
port shape as `notifications.register_transcript_recorder`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from assistant import messages as msg
from assistant.providers import get_provider
from common.timeutil import as_aware
from config.settings import settings
from domain.db import session_scope
from domain.models import (
    AbandonedCartNudge,
    CartItem,
    Channel,
    ChannelIdentity,
    Order,
    SessionRow,
    SilenceNudge,
    Variant,
    utcnow,
)
from domain.services import identities, notifications

log = logging.getLogger("rehla.nudges")

CAIRO = ZoneInfo("Africa/Cairo")
CHANNELS = ("whatsapp", Channel.INSTAGRAM_DM.value)
WINDOW = notifications.CUSTOMER_SERVICE_WINDOW
#: A deferred #2 must still land comfortably inside the window, not on its edge.
WINDOW_MARGIN = timedelta(minutes=15)

SENDING = "sending"
SENT = "sent"

FIRST_FALLBACK = "لسه معانا؟ 🙂 لو في أي حاجة أقدر أساعد حضرتك فيها أنا موجود."
FIRST_FALLBACK_PRODUCT = "لسه معانا؟ 🙂 لو حابين نكمل في {product} أنا موجود، ولو في أي سؤال تحت أمر حضرتك."
SECOND_TEXT = (
    "لسه {items} مستني في الشنطة 🛍️ نكمل الأوردر؟ ولو في أي سؤال قبلها أنا تحت أمر حضرتك."
)

FIRST_PROMPT = """\
إنت خدمة عملاء «رحلة» (براند لبس مصري). الزبون سكت بعد آخر رد مننا من حوالي ١٠ دقايق.
اكتب رسالة متابعة واحدة قصيرة جداً (جملة أو اتنين، أقل من ٢٠ كلمة) باللهجة المصرية:
- تسأل بلطف لو لسه معانا، وتشاور على اللي كان بيتكلم فيه لو واضح (اسم المنتج زي ما اتكتب).
- من غير ضغط ولا استعجال، ومن غير أسعار ولا أرقام ولا عروض.
- كلّمه بـ«حضرتك» وبصيغة محايدة، من غير ألقاب، وإيموجي واحد بالكتير.
- إنت بتكلّم الزبون نفسه مباشرة: متتكلمش عنه بصيغة الغايب («اللي بيسأل عنها») ومتعيدش تفاصيل الطلب كلها.
اكتب الرسالة نفسها بس، من غير أي شرح."""

_CLOSING = re.compile(
    r"(شكر|متشكر|ميرسي|تسلم|\bسلام\b|\bباي\b|مع السلامة|مش عايز|مش محتاج|مش حاب|لا شكرا"
    r"|\bthanks?\b|\bthank you\b|\bthx\b|\bbye\b|\bmersi\b|\bmerci\b|\bno thanks\b)",
    re.IGNORECASE,
)
#: Greetings carry «سلام» too, and «السلام عليكم» opens a conversation rather
#: than ending it -- found by the local end-to-end run, where the commonest
#: first message there is meant a general chat was never nudged. («خلاص» is
#: gone for the same reason: «خلاص هاخده» is a yes.)
_GREETING = re.compile(r"(ال)?سلام عليكم|وعليكم السلام")
_OPT_OUT = re.compile(
    r"(\bstop\b|\bunsubscribe\b|متبعتليش|ماتبعتليش|متبعتش|ماتبعتش|بطل[وي]? تبعت|بطّل تبعت"
    r"|مش عايز رسايل|مش عايزة رسايل|كفاية رسايل|الغي الاشتراك|إلغاء الاشتراك)",
    re.IGNORECASE,
)
_ARABIC = re.compile(r"[؀-ۿ]")
_DIGIT = re.compile(r"[0-9٠-٩]")


# -- clock --------------------------------------------------------------------


def in_quiet_hours(at: datetime) -> bool:
    start, end = settings.nudge_quiet_start_hour, settings.nudge_quiet_end_hour
    if start == end:
        return False
    hour = as_aware(at).astimezone(CAIRO).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def quiet_hours_end(at: datetime) -> datetime:
    """The first moment at or after `at` that is outside quiet hours."""
    local = as_aware(at).astimezone(CAIRO)
    end = local.replace(
        hour=settings.nudge_quiet_end_hour, minute=0, second=0, microsecond=0
    )
    if end <= local:
        end += timedelta(days=1)
    return end.astimezone(as_aware(at).tzinfo)


# -- reading the conversation -------------------------------------------------


def _live(row: SessionRow | None) -> list[dict]:
    if row is None or not isinstance(row.history, list):
        return []
    return [m for m in row.history[row.context_start or 0 :] if isinstance(m, dict)]


def _spoken(history: list[dict]) -> list[dict]:
    """User and assistant messages that say something -- no tool plumbing."""
    return [
        m
        for m in history
        if m.get("role") in (msg.USER, msg.ASSISTANT) and (m.get("content") or "").strip()
    ]


def _last_customer_text(history: list[dict]) -> str:
    for m in reversed(history):
        if m.get("role") == msg.USER:
            return m.get("content") or ""
    return ""


def closes_conversation(text: str) -> bool:
    return bool(_CLOSING.search(_GREETING.sub(" ", text or "")))


def opts_out(text: str) -> bool:
    return bool(_OPT_OUT.search(text or ""))


# -- the decision -------------------------------------------------------------


def _hard_skip(db, identity: ChannelIdentity, row: SilenceNudge, history: list[dict]) -> str | None:
    """A reason this silence gets no nudge at all, or None."""
    if row.opted_out_at is not None:
        return "opted_out"
    if identity.paused_until_staff_reply:
        return "handoff"
    last_said = _last_customer_text(history)
    if opts_out(last_said):
        row.opted_out_at = utcnow()
        return "opted_out"
    if closes_conversation(last_said):
        return "ended"
    anchor = as_aware(row.anchor_at)
    orders = db.scalars(
        select(Order.placed_at).where(
            Order.source_channel == identity.channel,
            Order.source_external_id == identity.external_id,
        )
    ).all()
    if any(as_aware(created) >= anchor - timedelta(minutes=1) for created in orders):
        return "order_placed"
    spoken = _spoken(history)
    if any(m.get("role") == msg.ASSISTANT and m.get("by") == "staff" for m in spoken[-6:]):
        return "staff_handled"
    return None


def _cart_items(db, channel: str, external_id: str) -> list[str]:
    rows = db.scalars(
        select(CartItem).where(CartItem.channel == channel, CartItem.external_id == external_id)
    ).all()
    names: list[str] = []
    for line in rows:
        variant = db.get(Variant, line.variant_id)
        name = variant.product.name if variant is not None else None
        if name and name not in names:
            names.append(name)
    return names


def _arm(db, channel: str, external_id: str, anchor: datetime) -> SilenceNudge | None:
    """The row for this identity, re-armed when the customer wrote again."""
    row = db.get(SilenceNudge, (channel, external_id))
    if row is None:
        try:
            with db.begin_nested():
                row = SilenceNudge(channel=channel, external_id=external_id, anchor_at=anchor)
                db.add(row)
        except IntegrityError:
            # Another worker created it first; theirs is the same anchor.
            db.expire_all()
            row = db.get(SilenceNudge, (channel, external_id))
        return row
    if as_aware(row.anchor_at) < anchor:
        # Conditional on the exact anchor just read, so a worker holding a
        # stale read cannot wipe a claim another worker made on the new one.
        db.execute(
            update(SilenceNudge)
            .where(
                SilenceNudge.channel == channel,
                SilenceNudge.external_id == external_id,
                SilenceNudge.anchor_at == row.anchor_at,
            )
            .values(
                anchor_at=anchor, first_state=None, first_at=None, second_state=None, second_at=None
            )
            .execution_options(synchronize_session=False)
        )
        db.expire(row)
        if as_aware(row.anchor_at) < anchor:
            return None
    return row


def _claim(db, channel: str, external_id: str, step: str, state: str) -> bool:
    """Atomically move `step` from undecided to `state`; False if beaten to it."""
    column = getattr(SilenceNudge, f"{step}_state")
    result = db.execute(
        update(SilenceNudge)
        .where(
            SilenceNudge.channel == channel,
            SilenceNudge.external_id == external_id,
            column.is_(None),
        )
        .values({f"{step}_state": state, f"{step}_at": utcnow()})
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _plan(db, identity: ChannelIdentity, now: datetime) -> tuple[str, str] | None:
    """What to do for one identity this tick: (step, "send"|skip-reason) or None."""
    channel, external_id = identity.channel, identity.external_id
    anchor = as_aware(identity.last_seen_at)
    row = _arm(db, channel, external_id, anchor)
    if row is None or (row.first_state is not None and row.second_state is not None):
        return None

    first_due = anchor + timedelta(minutes=settings.nudge_first_minutes)
    second_due = anchor + timedelta(hours=settings.nudge_second_hours)
    if now < first_due:
        return None

    history = _live(db.get(SessionRow, (channel, external_id)))
    spoken = _spoken(history)
    if not spoken or spoken[-1].get("role") == msg.USER:
        # Nothing said yet, or her message is still being answered (the
        # debounce window, a slow turn): not silence, so decide nothing.
        return None

    reason = _hard_skip(db, identity, row, history)
    if reason:
        step = "first" if row.first_state is None else "second"
        if step == "first":
            row.second_state = row.second_state or reason
        return (step, reason)

    if row.first_state is None:
        if now >= second_due:
            return ("first", "stale")  # missed (downtime): straight on to #2
        last = spoken[-1]
        if last.get("by") is not None:
            # The last word is a system push (a status update, a resolution)
            # rather than a reply to her -- not a silence after our answer.
            return ("first", "not_bot_reply")
        if in_quiet_hours(now):
            return ("first", "quiet_hours")
        return ("first", "send")

    if row.second_state is None and now >= second_due:
        if not _cart_items(db, channel, external_id):
            return ("second", "no_cart")
        legacy = db.get(AbandonedCartNudge, (channel, external_id))
        if legacy is not None and as_aware(legacy.sent_at) >= anchor:
            # The abandoned-cart job this replaced already nudged this
            # silence (only possible across the deploy that switched over).
            return ("second", "already_nudged")
        if now >= anchor + WINDOW - WINDOW_MARGIN:
            return ("second", "window_closed")
        if in_quiet_hours(now):
            if quiet_hours_end(now) < anchor + WINDOW - WINDOW_MARGIN:
                return None  # deferred: picked up when quiet hours end
            return ("second", "quiet_hours")
        return ("second", "send")
    return None


# -- the words ----------------------------------------------------------------


def _acceptable(text: str) -> bool:
    from assistant.reply_rules import not_egyptian

    return (
        5 <= len(text) <= 220
        and text.count("\n") <= 1
        and bool(_ARABIC.search(text))
        and not _DIGIT.search(text)
        and not not_egyptian(text)
    )


def first_text(history: list[dict]) -> str:
    """Nudge #1 in the brand voice, from the conversation; a fixed line on any doubt."""
    from assistant.reply_rules import fix_arabic
    from assistant.tools.base import last_product

    product = last_product(history)
    name = (product or {}).get("name")
    fallback = FIRST_FALLBACK_PRODUCT.format(product=name) if name else FIRST_FALLBACK
    spoken = _spoken(history)[-10:]
    if not spoken:
        return fallback
    transcript = "\n".join(
        f"{'الزبون' if m['role'] == msg.USER else 'رحلة'}: {m['content'].strip()[:400]}"
        for m in spoken
    )
    try:
        reply = get_provider().generate(
            FIRST_PROMPT, [{"role": msg.USER, "content": transcript}], []
        )
        text = (reply.text or "").strip().strip('"«»').strip()
        text, _ = fix_arabic(text)
    except Exception:
        log.warning("nudge #1 wording failed; using the fixed line", exc_info=True)
        return fallback
    if reply.tool_calls or not _acceptable(text):
        log.info("nudge #1 wording refused (%r); using the fixed line", text[:80])
        return fallback
    return text


def second_text(items: list[str]) -> str:
    return SECOND_TEXT.format(items=" و".join(items) if items else "طلب حضرتك")


# -- the poll -----------------------------------------------------------------


def check_silences(now: datetime | None = None) -> int:
    """One pass over every identity heard from in the last 24 hours. Returns sends."""
    now = now or utcnow()
    with session_scope() as db:
        candidates = [
            (i.channel, i.external_id)
            for i in db.scalars(
                select(ChannelIdentity).where(ChannelIdentity.channel.in_(CHANNELS))
            ).all()
            # Compared in Python, as reengagement did: see its note on
            # tz-aware comparisons across SQLite and PostgreSQL.
            if now - as_aware(i.last_seen_at) < WINDOW
        ]

    sent = 0
    for channel, external_id in candidates:
        try:
            sent += _handle(channel, external_id, now)
        except Exception:
            log.exception("silence nudge failed for %s/%s", channel, external_id)
    return sent


def _handle(channel: str, external_id: str, now: datetime) -> int:
    if not notifications.has_sender(channel):
        return 0
    with session_scope() as db:
        identity = identities.get(db, channel, external_id)
        if identity is None:
            return 0
        anchor = as_aware(identity.last_seen_at)
        decision = _plan(db, identity, now)
        if decision is None:
            return 0
        step, verdict = decision
        if verdict != "send":
            _claim(db, channel, external_id, step, f"skipped:{verdict}")
            log.info("nudge #%s for %s skipped: %s", 1 if step == "first" else 2, external_id, verdict)
            return 0
        if not _claim(db, channel, external_id, step, SENDING):
            return 0  # another worker has it
        history = _live(db.get(SessionRow, (channel, external_id)))
        items = _cart_items(db, channel, external_id) if step == "second" else []
    # Committed: the step is ours. The wording (a model call for #1) happens
    # outside any transaction.
    text = first_text(history) if step == "first" else second_text(items)

    with session_scope() as db:
        identity = identities.get(db, channel, external_id)
        row = db.get(SilenceNudge, (channel, external_id))
        if (
            identity is None
            or as_aware(identity.last_seen_at) > anchor  # she wrote while we were wording it
            or identity.paused_until_staff_reply
        ):
            setattr(row, f"{step}_state", "skipped:customer_returned")
            return 0
        delivered = notifications.send_nudge(db, channel, external_id, text)
        setattr(row, f"{step}_state", SENT if delivered else "failed")
        setattr(row, f"{step}_at", utcnow())
    log.info("nudge #%s to %s/%s: %s", 1 if step == "first" else 2, channel, external_id,
             "sent" if delivered else "not delivered")
    return 1 if delivered else 0
