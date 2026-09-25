"""Whether this turn should ask the customer's name, and what to call them.

A conversation used to be titled on the dashboard by a phone number (or, on
Instagram, a handle) until the customer placed an order, because the only
name anywhere was `Client.full_name`, written at checkout. Most conversations
never reach checkout, so most of the inbox was numbers.

The bot now asks, once, near the start of a conversation. What is decided
here, in code, is *whether* to ask -- never the wording, which is the model's
so it can sit naturally at the end of an answer instead of in front of one:

* **never when the name is known** -- from an order (`Client.full_name`) or
  from an earlier answer (`ChannelIdentity.customer_name`);
* **once per conversation.** A customer who read the question and carried on
  without answering it has answered it -- for this conversation. Whether it
  was asked is read off the replies of the *live* conversation, never the
  archive: the first version read the whole transcript, so a number whose
  history held any earlier «اسم حضرتك» (checkout asks it) was "already asked"
  forever, and a staff reset -- which archives, it does not erase -- did not
  make it a new conversation. That is how production's first greeting after a
  reset, on 2026-09-25, went out with no name question at all;
* **only in a conversation's first few replies** (`ASK_WITHIN_REPLIES`). The
  name is an introduction; asked in the middle of choosing a size it is a
  form field, and checkout asks for the name the parcel goes to anyway.

And it is **not left to the model to remember**. The note asks for the
question to sit after the answer, in the model's own words; if the finished
reply still does not ask, `ensure_asked` adds the one line itself -- after
everything else, so an answer is never held behind it -- except on an
apology, where a name question reads as not having listened. Every turn logs
what was decided (`log_decision`), because the only trace the first version
left in production was the absence of a question.

The answer is saved by the `save_customer_name` tool, which refuses a name
the customer never typed.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from assistant.messages import ASSISTANT
from domain.services import identities

log = logging.getLogger("wanas.customer_name")

#: What `ensure_asked` adds when the model answered without asking.
ASK_LINE = "ممكن أعرف اسم حضرتك؟"

#: A reply that apologises is about something that went wrong for the
#: customer; a name question under it reads as not having listened.
_APOLOGY = re.compile(r"آسف|اسف|معلش|أعتذر|اعتذر|للأسف|للاسف|عذرًا|عذراً|عذرا")

#: The bot's replies, counted from the start of the live conversation, within
#: which the name is still an introduction. Past it the question is dropped
#: for this conversation rather than asked out of nowhere.
ASK_WITHIN_REPLIES = 3

#: The phrasings a reply asking for a name actually uses. Deliberately broad:
#: a false positive only means the bot does not ask a second time, which is
#: the safe direction.
_ASKS_FOR_NAME = re.compile(
    r"اسم\s*(حضرتك|ك\b|كم\b|ك\s*[؟?]|ك\s+(ايه|إيه|اية|إية))"
    r"|اسمك|أتشرف\s+باسم|اتشرف\s+باسم|نتشرف\s+باسم|نتعرف\s+على\s+حضرتك"
    r"|أناديك|اناديك|أنادي\s+حضرتك|your\s+name",
    re.IGNORECASE,
)

_ASK_NOTE = """

# اسم الزبون
لسه منعرفش اسم الزبون ده. اطلبه في الرد ده بشكل طبيعي، زي موظف بيتعرف على زبون — مش استمارة:
- رسالته تحية بس: رد على تحيته هو (سلام بسلام، صباح بصباح، «عامل إيه» بـ«الحمد لله») واسأله عن اسمه في نفس الرسالة: «أهلاً بحضرتك في Wanas Gallery، ممكن أعرف اسم حضرتك؟».
- سأل سؤال أو طلب حاجة: **جاوبه الأول كامل**، وبعدين سطر أخير قصير «وممكن أعرف اسم حضرتك؟». الاسم عمره ما ييجي قبل الإجابة ولا بدلها.
- لو ردك لازم يخلص بسؤال عن الطلب نفسه (مقاس، لون، عنوان)، أو الزبون متضايق أو بيشتكي — متسألش عن الاسم في الرد ده.
- لو قال اسمه في رسالته، نادي save_customer_name ومتسألوش."""

_KNOWN_NOTE = """

# اسم الزبون
اسم الزبون «{name}». متسألوش عن اسمه تاني. ممكن تناديه بيه من غير لقب في الترحيب أو التأكيد — مرة كل فين وفين، مش في كل رسالة. ولما ييجي وقت الأوردر، أكّد الاسم ده بدل ما تسأل عليه («نسجل الأوردر باسم {name}؟»)، ولو ده اسم أول بس اطلب الاسم بالكامل عشان المندوب."""


def asks_for_name(text: str) -> bool:
    """Whether a reply asks the customer what their name is."""
    return bool(_ASKS_FOR_NAME.search(text or ""))


def _replies(history: list[dict]) -> list[str]:
    """What the shop said, reply by reply -- an assistant message that
    carries text, whether or not it also made a tool call."""
    return [
        m.get("content") or ""
        for m in history
        if isinstance(m, dict) and m.get("role") == ASSISTANT and (m.get("content") or "").strip()
    ]


def already_asked(history: list[dict]) -> bool:
    """Whether a reply in *this* conversation (the live slice) asked the name."""
    return any(asks_for_name(text) for text in _replies(history))


def decide(db: Session, channel: str, external_id: str, history: list[dict]) -> str:
    """What this turn does about the name, as one word:

    `known` -- we hold it; `asked` -- asked earlier in this conversation;
    `late` -- past the conversation's opening replies; `ask` -- ask now.
    """
    if identities.known_name(db, channel, external_id):
        return "known"
    if already_asked(history):
        return "asked"
    if len(_replies(history)) >= ASK_WITHIN_REPLIES:
        return "late"
    return "ask"


def should_ask(db: Session, channel: str, external_id: str, history: list[dict]) -> bool:
    """Ask on this turn? See the module docstring for each condition."""
    return decide(db, channel, external_id, history) == "ask"


def turn_note(db: Session, channel: str, external_id: str, history: list[dict]) -> str:
    """The paragraph this turn's system prompt carries about the name, or ""."""
    name = identities.known_name(db, channel, external_id)
    if name:
        # Validated against the customer's own words when it was saved, and
        # quoted rather than spliced in bare.
        return _KNOWN_NOTE.format(name=name.replace("«", "").replace("»", ""))
    if should_ask(db, channel, external_id, history):
        return _ASK_NOTE
    return ""


def log_decision(channel: str, external_id: str, decision: str) -> None:
    """One line per turn saying what the name logic decided -- never the
    name itself, which is a customer's personal data in a hosting log."""
    log.info("customer name for %s/%s: %s", channel, external_id, decision)


def ensure_asked(text: str, decision: str) -> str:
    """The reply, with the name question added if this turn had to ask and
    the model's reply does not. Added last, so the answer still comes first."""
    if decision != "ask" or not (text or "").strip():
        return text
    if asks_for_name(text) or _APOLOGY.search(text):
        return text
    log.info("the reply did not ask the customer's name; adding the question")
    return text.rstrip() + "\n\n" + ASK_LINE
