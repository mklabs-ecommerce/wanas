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
* **once, ever.** A customer who read the question and carried on without
  answering it has answered it. Whether it was asked is read off the stored
  transcript (every reply the shop sent, archive included), not off a flag the
  model would have to remember to set;
* **only in a conversation's first few replies** (`ASK_WITHIN_REPLIES`). The
  name is an introduction; asked in the middle of choosing a size it is a
  form field, and checkout asks for the name the parcel goes to anyway.

The answer is saved by the `save_customer_name` tool, which refuses a name
the customer never typed.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from assistant import session as session_store
from assistant.messages import ASSISTANT
from domain.services import identities

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
- رسالته سلام أو ترحيب بس: رد السلام واسأله عن اسمه في نفس الرسالة، مثلاً «وعليكم السلام، أهلاً بحضرتك في Wanas Gallery. ممكن أعرف اسم حضرتك؟».
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


def already_asked(db: Session, channel: str, external_id: str) -> bool:
    """Whether any reply the shop ever sent this customer asked their name."""
    transcript = session_store.transcript(db, channel, external_id)
    return any(asks_for_name(text) for text in _replies(transcript))


def should_ask(db: Session, channel: str, external_id: str, history: list[dict]) -> bool:
    """Ask on this turn? See the module docstring for each condition."""
    if identities.known_name(db, channel, external_id):
        return False
    if len(_replies(history)) >= ASK_WITHIN_REPLIES:
        return False
    return not already_asked(db, channel, external_id)


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
