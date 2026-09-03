"""The agent loop.

1. Load conversation history for this channel identity.
2. Append the incoming message.
3. Call the model with the system prompt, the history and the tool schemas.
4. No tool calls -> that is the reply; save and send.
5. Tool calls -> execute all of them, append the results, go back to 3.
6. Cap the loop.

All of a turn's tool calls are executed together rather than one per round
trip, because "the black hoodie in L and the olive polo in M" is one message
with two products and should resolve in one pass.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from assistant import context, messages as msg, quoting, session as session_store
from assistant.prompt import build_system_prompt
from assistant.providers import LLMProvider, ProviderError, get_provider
from assistant.tools.base import (
    REGISTRY,
    ToolContext,
    call_tool,
    last_product,
    load_all,
    tool_specs,
)
from config.settings import settings

log = logging.getLogger("wanas.agent")

load_all()

# Customer-facing failure text. The customer never sees a stack trace and
# staff always see the real error.
RATE_LIMITED = "الضغط عالي شوية، ابعتلي تاني بعد دقيقة."
GENERIC_FAILURE = "حصلت مشكلة عندنا دلوقتي. جرب تبعت تاني بعد شوية وأنا موجود."
LOOP_EXHAUSTED = "معلش خدت وقت في ده. ممكن توضحلي طلبك في جملة واحدة؟"

#: How long to wait before retrying one failed model call. Same figure as
#: `assistant/turn_retry.py`'s whole-turn retry, and the same reasoning: long
#: enough for a rate limit or a dropped connection to actually clear, short
#: enough that the customer is not kept waiting for nothing.
MODEL_RETRY_DELAY_SECONDS = 30.0

#: Seam for tests: replaced with a no-op so the retry path can be proven to
#: run without a suite actually sleeping thirty seconds.
_sleep = time.sleep

#: `ProviderError` kinds worth retrying once. `auth` is deliberately absent --
#: a rejected key or missing configuration will be rejected identically
#: thirty seconds later, and every second spent waiting is a second the
#: customer goes unanswered for a problem no retry fixes; it falls straight
#: through to the existing `except ProviderError` handling below, unretried,
#: exactly as before this shipped.
_TRANSIENT_PROVIDER_ERROR_KINDS = {"rate_limit", "provider_error"}


#: Any local path the model might echo out of a tool result. The prompt tells
#: it never to write one; this is the guarantee behind the instruction, since
#: a pasted "data/images/..." is meaningless to a customer and leaks the
#: server's layout.
_PATH_LEAK = re.compile(r"\S*data[/\\](?:images|size-charts|inbound)[/\\]\S*")


def strip_paths(text: str) -> tuple[str, bool]:
    cleaned = _PATH_LEAK.sub("", text or "")
    if cleaned == text:
        return text, False
    # Collapse the whitespace the removal left behind.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True


#: A tool the model named as if it had called it, written out as text instead
#: of a real function call -- `request_human(reason='unclear', summary='...')`
#: as the actual reply. The prompt forbids it; this is the guarantee behind
#: the instruction, built from the live tool registry so it never drifts from
#: what the model can actually call.
def _tool_call_leak_pattern() -> re.Pattern:
    names = sorted(REGISTRY, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\s*\([^)]{0,400}\)")


_TOOL_CALL_LEAK = _tool_call_leak_pattern()


def strip_tool_leaks(text: str) -> tuple[str, bool]:
    cleaned = _TOOL_CALL_LEAK.sub("", text or "")
    if cleaned == text:
        return text, False
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True


#: `**bold**`, `__bold__` and `#`/`##` headings -- Markdown the model reaches
#: for out of habit that makes a WhatsApp reply read like a generated
#: document instead of someone typing. Kept to the handful of forms that are
#: unambiguous to strip; a bare hyphen or asterisk *inside* a sentence is left
#: alone; it could be a real part of it.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)

#: A Markdown list marker at the *start of a line*, rewritten to the bullet
#: character rather than deleted. The prompt asks for lists now -- products,
#: available colours, what is still missing from an order -- because a wall of
#: names in one sentence is what a customer stops reading. Neither WhatsApp
#: nor Instagram renders `-` or `*` into anything, though: they arrive as the
#: literal character, which reads like a typo. So the shape the model reaches
#: for out of habit is normalised into the one the customer sees correctly,
#: instead of being forbidden and then leaking through anyway. A `-` mid
#: sentence is untouched, and so is a line whose marker has no space after it
#: (a negative number, a franco word) -- only "marker, space, text" is a list.
_MD_BULLET = re.compile(r"^([ \t]{0,4})[-*][ \t]+(?=\S)", re.MULTILINE)


def strip_markdown(text: str) -> tuple[str, bool]:
    cleaned = _MD_BOLD.sub(lambda m: m.group(1) or m.group(2) or "", text or "")
    cleaned = _MD_HEADING.sub("", cleaned)
    cleaned = _MD_BULLET.sub(r"\1• ", cleaned)
    if cleaned == text:
        return text, False
    return cleaned.strip(), True


#: "I'll tell you now" / "let me get back to you" said as the entire reply,
#: with nothing behind it -- the exact bug this guards: the model promises a
#: follow-up («ثواني هشوفلك المتاح», «هقولك دلوقتي») and then the turn just
#: ends, because a reply with no tool_calls *is* the final answer as far as
#: the loop in run_turn is concerned. There is no second turn: nothing in this
#: system ever wakes up to finish a promise, so a promise that reaches the
#: customer is dead air by construction.
#:
#: Two families, because the model phrases it both ways: an explicit future
#: verb («هشوفلك», «هراجع», «هتأكدلك», "I'll check") and a bare waiting marker
#: with no verb at all («ثواني», «لحظة», "one moment"). The old pattern only
#: listed a handful of future verbs and matched none of the stock-check
#: wordings the bot actually uses, which is why this kept happening.
_PROMISE_VERBS = (
    r"(?:قول|شوف|بص|راجع|تأكد|تاكد|أكد|اكد|رد|بعت|جيب|وضح|رجع|عرف|كلم|شيك|تشيك|دور|سأل|اسأل|جبلك)"
)
_PROMISE_PATTERN = re.compile(
    # ه/ها/هت + verb («هشوفلك», «هتأكدلك», «هاقولك»), and the present-tense
    # «بشوف/بتأكد» form that means the same thing.
    rf"\bه[اأ]?ت?{_PROMISE_VERBS}"
    rf"|\bب{_PROMISE_VERBS}لك"
    rf"|\bأ{_PROMISE_VERBS}لك"
    # Bare waiting markers, no verb needed.
    r"|ثوان|ثانية|لحظة|لحظه|دقيقة واحدة|استنى|إستنى|انتظر|جاري ال"
    r"|i'?ll (get back|check|tell you|let you know|find out|confirm)"
    r"|let me (check|get back|see|find out|confirm)"
    r"|(one|just a) (moment|sec|second)|hold on|checking (now|on that)|bear with me"
)

#: A reply that actually delivered something: a number (a price, a quantity, a
#: size), or a verdict the customer can act on. Used only to keep a *long*
#: substantive reply that happens to contain «هقولك» from being treated as a
#: promise.
_SUBSTANCE = re.compile(r"\d|مش متاح|مش متوفر|خلص|نفد|sold out|out of stock")

#: A reply the model did not finish writing. The generation stopped because it
#: ran into the completion ceiling, not because the sentence ended, so what
#: came back is cut off wherever the token counter stopped -- mid-word,
#: mid-number, mid-question. `finish_reason` is the only thing that
#: distinguishes it from a finished reply: the text itself reads as ordinary
#: Arabic right up to the point it stops making sense, which is exactly how it
#: reached customers -- an order summary closing with a half-written
#: "اطمأت وأنا أسجله؟".
#:
#: Two spellings because the field is the upstream provider's, passed through
#: untranslated: OpenAI-compatible stacks say `length`, some say `max_tokens`,
#: and Gemini says `MAX_TOKENS`. Compared lower-cased.
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})

#: How many times a truncated reply is regenerated before the deterministic
#: fallback is used instead. Same shape as the promise retries below and for
#: the same reason: the model usually finishes when asked for something
#: shorter, and a customer cannot be kept waiting forever if it does not.
_TRUNCATION_RETRY_LIMIT = 2

#: Appended to the system prompt when a reply came back cut off. Never stored:
#: like the promise nudges, the point is that the model does not read its own
#: broken output back, only the instruction to write something that fits.
_TRUNCATION_NUDGE = (
    "\n\nتنبيه داخلي: ردك اللي فات اتقطع في نصه لأنه طويل أوي. "
    "اكتبه تاني أقصر بكتير -- أهم معلومة بس، في جملتين أو تلاتة على الأكتر، وخلي الجملة الأخيرة كاملة."
)

#: Sent instead of half a sentence. It says the one true thing -- the message
#: did not come out whole -- and asks for the one thing that lets the next turn
#: answer, so it needs nothing following it to make sense. The same standard
#: `PROMISE_FALLBACK` is held to.
TRUNCATED_FALLBACK = (
    "معلش، الرسالة اتقطعت مني. ممكن تقوللي تاني عايز إيه بالظبط وأنا أرد عليك فورًا؟"
)


def _is_truncated(reply) -> bool:
    """Whether the model stopped because it ran out of budget.

    Structural, like `_is_dangling_promise`'s "did a tool actually run":
    there is nothing in the *wording* of a cut-off reply to detect -- it is
    real Arabic that simply stops -- so the provider's own `finish_reason` is
    the whole signal, and the only one available.
    """
    reason = str(getattr(reply, "finish_reason", "") or "").strip().lower()
    return reason in TRUNCATED_FINISH_REASONS


#: How many times a dangling promise is sent back to the model before the
#: deterministic fallback is used instead. Never 0 retries (the model usually
#: gets it right when told) and never unbounded (a customer is waiting).
_PROMISE_RETRY_LIMIT = 2

#: The last resort, when the model will not stop promising. Sent *instead of*
#: the promise: it asks for the one thing that lets the next turn answer for
#: real, and -- unlike the promise -- it needs no follow-up from us to make
#: sense. Dead air is the failure; a question is not.
PROMISE_FALLBACK = (
    "معلش، قولي اسم المنتج واللون والمقاس اللي عايزه بالظبط وأقولك المتاح منه فورًا."
)

#: The same last resort for a conversation that already established what it is
#: about. Asking a customer to restate a product the bot itself named two
#: messages ago is the part that reads as amnesia -- and it is the fallback
#: doing it, not the model, because a constant carries no context by
#: construction. These ask only for what is genuinely still missing.
PROMISE_FALLBACK_WITH_PRODUCT = (
    "معلش، بالنسبة لـ {product} — قوللي اللون والمقاس اللي عايزهم وأقولك المتاح منه فورًا."
)
PROMISE_FALLBACK_WITH_COLOR = (
    "معلش، بالنسبة لـ {product} {color} — قوللي المقاس اللي عايزه وأقولك المتاح منه فورًا."
)


def promise_fallback(history: list[dict]) -> str:
    """The last-resort question, narrowed by what the conversation settled.

    Deterministic: one read of the stored history through
    `tools.base.last_product`, no second model call and nothing summarised --
    this runs at the point where the model has already failed twice, so
    trusting it once more is the one thing that cannot be part of the answer.
    The product name is interpolated straight from the database row, never
    from a sentence a model wrote.

    Falls back to the context-free wording whenever no product has actually
    been looked up, which keeps the honest case honest: a conversation that
    never named a product must not be told what it was about.
    """
    recent = last_product(history)
    if recent is None or not recent.get("name"):
        return PROMISE_FALLBACK
    if recent.get("color"):
        return PROMISE_FALLBACK_WITH_COLOR.format(product=recent["name"], color=recent["color"])
    return PROMISE_FALLBACK_WITH_PRODUCT.format(product=recent["name"])


def _is_dangling_promise(text: str, *, tools_called: bool) -> bool:
    """A final reply that promises a follow-up which will never come.

    The structural signal, not the wording, is what makes this reliable: a
    promise-shaped reply in a turn where **no tool ran** cannot possibly
    contain a looked-up answer, whatever it says. Widening the phrase list was
    the previous fix and it kept missing new phrasings; this fires on the
    invariant instead and uses the phrase list only to decide "promise-shaped".
    """
    if not text:
        return False
    words = len(text.split())
    if words > 40:
        # Long enough that it is a real answer with a courtesy line in it.
        return False
    if not _PROMISE_PATTERN.search(text.lower()):
        return False
    if not _SUBSTANCE.search(text.lower()):
        # Promise-shaped and nothing concrete in it: dead air whether or not a
        # tool ran, because the customer is left with no answer either way.
        return True
    # It does carry something concrete. Only treat it as dangling when it is
    # also very short *and* nothing was looked up -- "ثواني هشوفلك الـ 3
    # ألوان" is still a promise; a real answer that ends with a courtesy
    # line is not.
    return not tools_called and words <= 12


#: The word "photo", in the forms the model actually writes it. Deliberately
#: not paired with a sending verb: Arabic has too many ways to say it
#: («هبعتلك», «اتفضل», «دي», «جاية»), and every previous attempt to enumerate
#: a phrase list is what let the next phrasing through.
_IMAGE_WORD = re.compile(r"صور|صوره|صورة|\bphotos?\b|\bpictures?\b|\bimages?\b")

#: The one sanctioned reason to mention photos while sending none: the product
#: has none. The prompt asks for exactly this sentence, so it must not be
#: retried -- nudging the model off it is nudging it towards inventing a
#: picture.
_NO_IMAGES = re.compile(
    r"(?:مفيش|ما فيش|معندناش|معنديش|ملقيتش|مش متاح|مش موجود|no|don'?t have|do not have)"
    r"[^\n]{0,20}?(?:صور|صوره|صورة|photo|picture|image)"
)


def _promises_images(text: str) -> bool:
    """A reply that talks about photos in a turn that attached none.

    The caller checks the attachments; this only decides whether the sentence
    claims a picture is coming. Same invariant as `_is_dangling_promise` and
    the same reason behind it: no second message is ever produced for a turn,
    so "حاضر، هبعتلك صور كل الألوان" with an empty `attachments` list is not a
    slow reply, it is the last thing the customer hears. It happened twice in
    one conversation, both times after the model had been told the product was
    already shown and answered in words alone.

    Two exemptions, and both are cases where the reply is doing its job:
    a **question** about photos ("تحب تشوف أنهي لون؟") leaves the customer
    something to answer, which is never dead air; and telling them a product
    has no photos is the honest answer the prompt asks for.
    """
    if not text:
        return False
    lowered = text.lower()
    if not _IMAGE_WORD.search(lowered):
        return False
    if "؟" in text or "?" in text:
        return False
    return not _NO_IMAGES.search(lowered)


#: Appended to the system prompt when a dangling promise is caught -- never
#: stored in history, since the point is the model should not see its own bad
#: reply and repeat the pattern, only be told to actually act.
_PROMISE_NUDGE = (
    "\n\nتنبيه داخلي: ردك اللي فات كان مجرد وعد (زي 'ثواني هشوفلك' أو 'هقولك "
    "دلوقتي') من غير ما تقول المعلومة الحقيقية. مفيش رسالة تانية هتتبعت بعد "
    "ردك -- الرد ده هو آخر حاجة العميل هيشوفها في الدور ده، فالوعد معناه سكوت "
    "تام. نادي الأداة المناسبة دلوقتي (get_products أو get_variants للمتاح "
    "والمقاسات والألوان، أو get_size_chart لو الزبون مش عارف مقاسه) وجاوب "
    "بالمعلومة نفسها في نفس الرد."
)

#: The second, blunter attempt. Same content, no room left for a polite
#: "one moment" in front of it.
_PROMISE_NUDGE_FINAL = (
    "\n\nتنبيه أخير: ممنوع تمامًا أي جملة انتظار أو وعد ('ثواني'، 'هشوفلك'، "
    "'هقولك'، 'لحظة'). لازم ردك الجاي يبدأ بالمعلومة نفسها بعد ما تنادي "
    "get_variants/get_products/get_size_chart، أو يسأل العميل سؤال محدد لو "
    "ناقصك بيانات."
)

#: The photo version of the nudge. The failure it answers is specific enough
#: to name: the model had been told that a product it already showed does not
#: need fetching again, concluded that saying "here are the colours" was the
#: whole job, and sent a sentence with nothing attached to it. So this says
#: the one thing that fixes it -- a picture exists only if the call is made in
#: *this* reply -- and names the argument that sends all of them.
_IMAGE_NUDGE = (
    "\n\nتنبيه داخلي: ردك اللي فات قال إنك بتبعت صور، بس مفيش ولا صورة "
    "اتبعتت. الصور بتتبعت بس لما تنادي get_variants في نفس الرد ده، ومفيش "
    "رسالة تانية هتتبعت بعده. لو العميل طالب صور الألوان، نادي get_variants "
    "بـ more_images: true دلوقتي -- ده مسموح حتى لو المنتج اتعرض قبل كده. "
    "ولو المنتج ملوش صور، قول كده بصراحة بدل ما تقول إنك بعتها."
)

#: Sent instead of an image promise the model will not honour. Asks for the
#: one thing that lets the next turn send a real photo and, unlike the
#: promise, makes sense with nothing following it.
IMAGE_PROMISE_FALLBACK = (
    "معلش، قولي اسم المنتج واللون اللي عايز تشوفه وأبعتلك صورته على طول."
)


def _sent_images(history: list[dict]) -> set[str]:
    """Every image already delivered earlier in this conversation.

    Read from the `attachments` the agent stamped onto its own past replies
    (see `messages.assistant`), not re-derived from tool results -- a tool can
    return more photos than the image policy actually sent, and only what was
    actually sent may never be re-sent uninvited.
    """
    seen: set[str] = set()
    for message in history:
        if message.get("role") == msg.ASSISTANT:
            seen.update(message.get("attachments") or [])
    return seen


@dataclass
class AgentReply:
    text: str
    attachments: list[str] = field(default_factory=list)
    #: What each attached photo is of, keyed by path. Carried to the channel
    #: adapter so the platform message id a photo goes out as can be recorded
    #: against the thing it shows -- see `ToolContext.attachment_labels` and
    #: `assistant/quoting.py`. Empty for a reply that attached nothing.
    attachment_labels: dict[str, str] = field(default_factory=dict)
    #: A tappable picker a tool asked for, in the neutral shape
    #: `assistant/interactive.py` defines. The adapter decides whether it can
    #: send one; a channel that cannot just sends the text.
    interactive: dict | None = None
    #: Tool names called this turn, in order. Logged, and what the tests and
    #: the chat harness read.
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    #: True when the turn deliberately produced no text because the customer
    #: has already been answered by another path -- today only the order
    #: confirmation, which `domain/services/notifications.py` composes and
    #: sends itself. Silence with a reason, so the adapters do not log it as
    #: a customer left sitting in the dark.
    silent: bool = False


def _generate_with_retry(
    provider: LLMProvider,
    system_prompt: str,
    model_history: list[dict],
    specs,
    *,
    channel: str,
    external_id: str,
):
    """One model call, with a single silent retry for a transient failure.

    The two `except` clauses in `run_turn` below are the *existing*
    behaviour -- a rate limit becomes `RATE_LIMITED`, anything else becomes
    `GENERIC_FAILURE` -- and this function does not change what they do. It
    only changes when they are reached: today, on the very first failure;
    now, only after one retry thirty seconds later has *also* failed. A
    non-transient `ProviderError` (`auth`) is re-raised immediately, with no
    wait, since a retry cannot change its outcome.

    `system_prompt` and `model_history` are passed in rather than
    recomputed, so both attempts see the exact same request -- nothing about
    the conversation moves between them.
    """
    try:
        return provider.generate(system_prompt, model_history, specs)
    except ProviderError as exc:
        if exc.kind not in _TRANSIENT_PROVIDER_ERROR_KINDS:
            raise
        reason = exc.kind
    except Exception:
        # Unclassified -- a bug in translation, a library raising something
        # unexpected. Retrying will not fix a real bug, but it will not make
        # one worse either, and it is the only chance a flaky one-off (a
        # library hiccuping on a dropped connection) gets to resolve itself
        # before the customer sees a generic apology for what was actually
        # nothing.
        reason = "unclassified"

    log.warning(
        "provider %s failed (%s) for %s/%s; waiting %.0fs and retrying once before answering",
        provider.name,
        reason,
        channel,
        external_id,
        MODEL_RETRY_DELAY_SECONDS,
    )
    _sleep(MODEL_RETRY_DELAY_SECONDS)
    return provider.generate(system_prompt, model_history, specs)


def run_turn(
    db: Session,
    channel: str,
    external_id: str,
    text: str,
    *,
    provider: LLMProvider | None = None,
    images: list[str] | None = None,
    audio: list[str] | None = None,
    recorded_ids: set[str] | None = None,
    reply_to: list[str] | None = None,
    mids: list[str] | None = None,
    system_extra: str | None = None,
) -> AgentReply:
    provider = provider or get_provider()
    specs = tool_specs()
    # The surface shapes the prompt: WhatsApp's wording stays byte-identical
    # to what it has always been; Instagram gets its own lines (see
    # assistant/prompt.py). `system_extra` is appended for one turn only and
    # is never stored -- today the resume paragraph
    # (`assistant/recovery.py::RESUME_INSTRUCTION`), which the turn after this
    # one must not still be reading.
    system_prompt = build_system_prompt(system_extra, channel=channel)

    # The messages this turn is about were already written to the transcript
    # when they arrived, so staff could see the conversation before the bot
    # had said anything (`assistant/runtime.py::record_inbound`). Drop those
    # provisional copies: the canonical message appended below is the one that
    # carries the photo context and the reply-to annotations the model reads.
    # Where the stored transcript stood before this turn touched anything.
    # `history` below is an in-memory copy; a tool can write to the row while
    # the loop is still running (the order confirmation does), and without
    # this bookmark the save at the end of the turn would overwrite it. See
    # `assistant/session.py::save`.
    base = session_store.stored_length(db, channel, external_id)
    history = session_store.drop_provisional(
        session_store.load(db, channel, external_id), recorded_ids
    )
    sent_images = _sent_images(history)

    refers_to = None
    if reply_to:
        # A long-pressed "reply to this" pointing at something outside this
        # debounce batch -- almost always a message the bot itself sent. The
        # quoted message is looked up in the *whole* stored transcript, not
        # the live slice, because a customer may well reply to something said
        # before the current context window opened. Resolved here rather than
        # in the adapter so every channel that can quote gets it for free.
        transcript = session_store.transcript(db, channel, external_id)
        text = quoting.annotate(text, transcript, reply_to)
        # And, when the quote landed on a photo, which product that photo was
        # of -- stored on the message so `tools.base.last_product` sees the
        # customer pointing at a jacket as the conversation moving to the
        # jacket. The words alone cannot carry that: the id is in a tool call
        # compaction drops.
        refers_to = quoting.referenced_product(transcript, reply_to)
    # `images`/`audio` are the customer's own inbound photo(s)/voice note(s)
    # for this turn -- kept on the stored message for the dashboard (see
    # `assistant/messages.py::user`), never sent to the provider itself, so
    # this is not a second copy of what the model already read via `text`.
    history.append(msg.user(text, images=images, audio=audio, mids=mids, refers_to=refers_to))

    # `history` is the same list object the loop below appends to, so the
    # tool layer's duplicate-call cache and image de-dup both see this turn's
    # own calls as they happen, not only what was already saved.
    ctx = ToolContext(
        session=db,
        channel=channel,
        external_id=external_id,
        sent_images=sent_images,
        history=history,
    )
    called: list[str] = []
    promise_retries = 0
    truncation_retries = 0
    # The customer sent a picture of their own this turn, so every mention of
    # a photo in the reply is about *theirs* -- "وصلتني الصورة، دي أقرب حاجة
    # عندنا" is an answer, not an undelivered promise. Without this the
    # image-promise guard below would retry the one reply the media path
    # exists to produce. `images` is the raw attachment list; the automated
    # reading `media.py` folds into the text is the same signal seen from the
    # other side.
    customer_sent_a_photo = bool(images) or "قراءة آلية للصورة" in (text or "")

    for _turn in range(settings.tool_loop_cap):
        try:
            # What the model sees is a view of `history`, not `history`
            # itself: recent messages verbatim, older ones compacted down
            # to what was actually said. See `assistant/context.py` --
            # the stored conversation keeps everything either way.
            #
            # A transient failure here (a rate limit, a dropped connection)
            # gets one silent retry thirty seconds later before either of the
            # `except` clauses below ever sees it -- see
            # `_generate_with_retry`. Only a *second* failure reaches them.
            reply = _generate_with_retry(
                provider,
                system_prompt,
                context.for_model(history),
                specs,
                channel=channel,
                external_id=external_id,
            )
        except ProviderError as exc:
            # A rate limit is transient and worth telling the customer about.
            # An auth or configuration failure is a deployment problem that no
            # customer message will fix, so it gets a generic apology and a
            # loud log entry.
            log.error("provider %s failed (%s): %s", provider.name, exc.kind, exc)
            text_out = RATE_LIMITED if exc.kind == "rate_limit" else GENERIC_FAILURE
            if settings.chatbot_debug:
                text_out = f"{text_out}\n[debug] {exc.kind}: {exc}"
            session_store.save(db, channel, external_id, history, merge_since=base)
            return AgentReply(text=text_out, error=exc.kind, tool_calls=called)
        except Exception as exc:
            # Anything the provider did not classify -- a bug in translation, a
            # library raising something unexpected. It must still be logged
            # with a traceback *and* still answer the customer: an exception
            # escaping here means the WhatsApp adapter sends nothing at all,
            # which is a silent failure from the customer's side even though
            # the server logged it.
            log.exception("unexpected failure in provider %s", provider.name)
            text_out = GENERIC_FAILURE
            if settings.chatbot_debug:
                text_out = f"{text_out}\n[debug] {type(exc).__name__}: {exc}"
            session_store.save(db, channel, external_id, history, merge_since=base)
            return AgentReply(text=text_out, error="provider_crash", tool_calls=called)

        if not reply.tool_calls:
            if reply.text and _is_truncated(reply):
                # The model ran into the completion ceiling mid-sentence.
                # There is nothing in the text to catch this on, and it is the
                # last thing the customer hears -- a turn produces exactly one
                # reply, so a half-written one is the whole answer. Ask for a
                # shorter one; if it still will not fit, say so plainly rather
                # than send the fragment.
                if truncation_retries < _TRUNCATION_RETRY_LIMIT:
                    log.warning(
                        "truncated reply (finish_reason=%s, %d chars) from provider %s "
                        "for %s/%s, retry %d/%d",
                        reply.finish_reason,
                        len(reply.text),
                        provider.name,
                        channel,
                        external_id,
                        truncation_retries + 1,
                        _TRUNCATION_RETRY_LIMIT,
                    )
                    truncation_retries += 1
                    system_prompt = f"{system_prompt}{_TRUNCATION_NUDGE}"
                    continue

                log.error(
                    "provider %s kept truncating its reply for %s/%s after %d retries; "
                    "sending the fallback instead of half a sentence",
                    provider.name,
                    channel,
                    external_id,
                    _TRUNCATION_RETRY_LIMIT,
                )
                text_out = TRUNCATED_FALLBACK
                history.append(msg.assistant(text_out, attachments=ctx.attachments))
                session_store.save(db, channel, external_id, history, merge_since=base)
                return AgentReply(
                    text=text_out,
                    attachments=ctx.attachments,
                    attachment_labels=ctx.attachment_labels,
                    interactive=ctx.interactive,
                    tool_calls=called,
                    error="truncated",
                )

            if not reply.text:
                # Usually a token limit or a content filter, and invisible
                # otherwise.
                log.warning(
                    "empty model reply for %s/%s (finish_reason=%s)",
                    channel,
                    external_id,
                    reply.finish_reason,
                )
            text_out, path_leaked = strip_paths(reply.text)
            text_out, tool_leaked = strip_tool_leaks(text_out)
            text_out, _ = strip_markdown(text_out)

            # Saying a photo is on its way while attaching none is the same
            # failure as promising to go and check: the turn ends there, so
            # the sentence *is* the last thing the customer gets. It is
            # checked separately because the signal is structural rather than
            # verbal -- the attachments list, not the wording -- and because
            # what the model has to be told to fix it is different.
            image_promise = (
                not ctx.attachments
                and not customer_sent_a_photo
                and _promises_images(text_out)
            )

            if image_promise or _is_dangling_promise(text_out, tools_called=bool(called)):
                # A promise is never sent. Nothing in this system produces a
                # second message for a turn that already answered, so a
                # promise reaching the customer *is* the dead air. Retry with
                # a nudge the model can act on; if it still will not answer,
                # send the deterministic question instead of the promise.
                if promise_retries < _PROMISE_RETRY_LIMIT:
                    log.warning(
                        "%s from provider %s for %s/%s "
                        "(tools this turn: %s), retry %d/%d",
                        "photos promised but none attached" if image_promise else "dangling promise",
                        provider.name,
                        channel,
                        external_id,
                        called or "none",
                        promise_retries + 1,
                        _PROMISE_RETRY_LIMIT,
                    )
                    promise_retries += 1
                    nudge = (
                        _IMAGE_NUDGE
                        if image_promise
                        else _PROMISE_NUDGE
                        if promise_retries == 1
                        else _PROMISE_NUDGE_FINAL
                    )
                    # The model never sees its own bad reply (it is not
                    # appended to history), only the instruction to act.
                    system_prompt = f"{system_prompt}{nudge}"
                    continue

                log.error(
                    "provider %s kept promising a follow-up for %s/%s after %d retries; "
                    "sending the fallback question instead of dead air",
                    provider.name,
                    channel,
                    external_id,
                    _PROMISE_RETRY_LIMIT,
                )
                text_out = (
                    IMAGE_PROMISE_FALLBACK if image_promise else promise_fallback(history)
                )
                history.append(msg.assistant(text_out, attachments=ctx.attachments))
                session_store.save(db, channel, external_id, history, merge_since=base)
                return AgentReply(
                    text=text_out,
                    attachments=ctx.attachments,
                    attachment_labels=ctx.attachment_labels,
                    interactive=ctx.interactive,
                    tool_calls=called,
                    error="image_promise" if image_promise else "dangling_promise",
                )

            if path_leaked or tool_leaked:
                # Worth logging: it means the prompt or the tool descriptions
                # have drifted, and it is the earliest signal of that. A tool
                # call written out as text instead of made for real is worse
                # -- the customer would have read raw function syntax.
                log.warning(
                    "stripped a %s from a reply to %s/%s",
                    "tool call" if tool_leaked else "file path",
                    channel,
                    external_id,
                )
            history.append(msg.assistant(text_out, signature=reply.signature, attachments=ctx.attachments))
            session_store.save(db, channel, external_id, history, merge_since=base)
            return AgentReply(
                text=text_out or GENERIC_FAILURE,
                attachments=ctx.attachments,
                attachment_labels=ctx.attachment_labels,
                interactive=ctx.interactive,
                tool_calls=called,
            )

        if _is_truncated(reply) and truncation_retries < _TRUNCATION_RETRY_LIMIT:
            # Tool calls from a generation that ran out of budget mid-write.
            # What survives is whatever fitted, and a call the model had not
            # finished specifying is a lookup for something it did not mean --
            # the wrong-product answer, reached from a new direction. Nothing
            # has run yet, so the hop is discarded and asked for again;
            # `_parse` already refuses the commoner case, where the cut lands
            # inside the arguments JSON and leaves it unparseable.
            log.warning(
                "truncated tool-call hop (finish_reason=%s, calls=%s) from provider %s "
                "for %s/%s, retry %d/%d",
                reply.finish_reason,
                [c.get("name") for c in reply.tool_calls],
                provider.name,
                channel,
                external_id,
                truncation_retries + 1,
                _TRUNCATION_RETRY_LIMIT,
            )
            truncation_retries += 1
            system_prompt = f"{system_prompt}{_TRUNCATION_NUDGE}"
            continue

        # The signatures ride along in the history and are handed straight
        # back to the provider on the next pass. Nothing here reads them --
        # the history is stored as JSON, so an opaque string survives the
        # database round-trip untouched, which is what makes a second tool
        # call in the same conversation work.
        history.append(msg.assistant(reply.text, reply.tool_calls, signature=reply.signature))

        results = []
        for call in reply.tool_calls:
            name = call.get("name", "")
            called.append(name)
            content = call_tool(ctx, name, call.get("arguments"))
            log.info("tool %s(%s) -> %s", name, call.get("arguments"), list(content)[:4])
            results.append(msg.tool_result(call.get("id", name), name, content))
        history.append(msg.tool_results(results))

        if ctx.end_turn:
            # A tool has already sent the customer the message this turn is
            # about. Anything the model added on top would be a second one --
            # see `ToolContext.end_turn`.
            log.info(
                "ending the turn for %s/%s after %s: the customer has already been "
                "answered by the tool itself",
                channel,
                external_id,
                ctx.end_turn,
            )
            session_store.save(db, channel, external_id, history, merge_since=base)
            return AgentReply(
                text="",
                attachments=ctx.attachments,
                attachment_labels=ctx.attachment_labels,
                interactive=ctx.interactive,
                tool_calls=called,
                silent=True,
            )

    # A model stuck calling tools without ever replying must hit a ceiling and
    # return something graceful, not spin.
    log.warning("tool loop cap hit for %s/%s after %s calls", channel, external_id, len(called))
    history.append(msg.assistant(LOOP_EXHAUSTED, attachments=ctx.attachments))
    session_store.save(db, channel, external_id, history, merge_since=base)
    return AgentReply(
        text=LOOP_EXHAUSTED,
        attachments=ctx.attachments,
        attachment_labels=ctx.attachment_labels,
        interactive=ctx.interactive,
        tool_calls=called,
        error="loop_cap",
    )
