"""Every number a reply states about money or a measurement came from a tool.

`AGENTS.md` has said since the first version that every fact in every reply
comes from a tool result, and the prompt says it in as many words («كل رقم
وكل معلومة بتقولها لازم تكون جاية من نتيجة أداة»). Nothing checked it. A
price the model remembered, a total it added up, a centimetre figure it
estimated -- each read exactly like one it had looked up, and on cash on
delivery the first two are argued about at the door while the third is a
return.

So the reply is read before it leaves:

* a **money amount** is a number in a clause that names the currency
  («جنيه», «EGP»), or a number directly after a price word («بـ», «السعر»);
* a **measurement** is a number directly followed by «سم» / «cm».

Each one has to appear somewhere the shop said it: a tool result in this
conversation, the customer's own messages (a budget they named is theirs to
have repeated), or one of the shop's published constants -- the shipping
fees in the rate table and the exchange surcharge. Anything else is a number
the model made, and the turn is sent back to fetch it or drop it.

What this does not judge is phrasing, and it never rewrites a number: a
wrong figure corrected by code is still a figure nobody looked up.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from assistant.messages import TOOL_RESULTS, USER

_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")

#: The currency, as a reply writes it.
_MONEY_WORD = re.compile(r"جنيه|جنية|جنيهات|ج\.م|\bجم\b|\begp\b|\ble\b|l\.e|\bpounds?\b", re.I)

#: A price word straight before a number: «بـ 580», «السعر 580», «بسعر 580».
_PRICE_BEFORE = re.compile(
    r"(?:\bبـ?|بسعر|السعر|سعره|سعرها|الإجمالي|الاجمالي|إجمالي|اجمالي|المجموع|الشحن|total|price)"
    r"\s*[:：-]?\s*$",
    re.I,
)

#: A measurement unit straight after a number.
_CM_AFTER = re.compile(r"^\s*(?:سم|سنتي|سنتيمتر|cm)\b", re.I)

#: What a number next to these is, when it is not money: a count of days,
#: hours, pieces, a percentage, a size. Checked straight after the number.
_NOT_MONEY_AFTER = re.compile(
    r"^\s*(?:%|٪|يوم|ايام|أيام|ساع|قطع|قطعة|حتة|حتت|مقاس|سم|cm|x\b|×)", re.I
)

#: A number that is an identifier rather than an amount: an order reference,
#: an internal order id, a phone number.
_ID_BEFORE = re.compile(r"(?:#|WNS-|رقم\s*(?:الأوردر|الاوردر|الطلب)?\s*)$", re.I)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CLAUSE = re.compile(r"[\n.،؛!؟?]+")

#: Below this a number in a money clause is a quantity or a count ("2 قطع
#: بـ 1160 جنيه"), not an amount -- nothing this shop sells costs under 20.
_SMALLEST_AMOUNT = Decimal(20)

#: Phone numbers and long references are not amounts either.
_LONGEST_AMOUNT_DIGITS = 6


def _decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    # One spelling per amount, so 580, 580.0 and "580.00" are the same fact --
    # and never `normalize()`'s 5.8E+2, which no reply is written in.
    return value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()


def _numbers_in(value, out: set[Decimal]) -> None:
    """Every number inside a tool result, however deeply it sits."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float, Decimal)):
        found = _decimal(str(value))
        if found is not None:
            out.add(found)
        return
    if isinstance(value, str):
        for raw in _NUMBER.findall(value.translate(_ARABIC_INDIC)):
            found = _decimal(raw)
            if found is not None:
                out.add(found)
        return
    if isinstance(value, dict):
        for item in value.values():
            _numbers_in(item, out)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _numbers_in(item, out)


def known_numbers(history: list[dict], constants: set[Decimal] | None = None) -> set[Decimal]:
    """Every number this conversation's tools returned or its customer typed."""
    found: set[Decimal] = set(constants or ())
    for message in history:
        role = message.get("role")
        if role == TOOL_RESULTS:
            for result in message.get("results") or []:
                _numbers_in(result.get("content"), found)
        elif role == USER:
            _numbers_in(message.get("content") or "", found)
    return found


def shop_constants(session) -> set[Decimal]:
    """The amounts the shop publishes without a lookup: its shipping fees and
    the exchange surcharge. Read from where they are kept, never restated."""
    from domain.models import ShippingRate
    from domain.services.orders import EXCHANGE_SURCHARGE

    found = {_decimal(str(EXCHANGE_SURCHARGE))}
    for (fee,) in session.query(ShippingRate.fee).filter(ShippingRate.fee.is_not(None)).all():
        found.add(_decimal(str(fee)))
        # Refusing a shipped parcel at the door costs the round trip.
        found.add(_decimal(str(Decimal(str(fee)) * 2)))
    return {value for value in found if value is not None}


def stated(text: str) -> tuple[list[Decimal], list[Decimal]]:
    """`(money, measurements)` a reply states, in the order it states them."""
    money: list[Decimal] = []
    measures: list[Decimal] = []
    folded = (text or "").translate(_ARABIC_INDIC)
    for clause in _CLAUSE.split(folded):
        money_clause = bool(_MONEY_WORD.search(clause))
        for match in _NUMBER.finditer(clause):
            raw = match.group(0).rstrip(",")
            value = _decimal(raw)
            if value is None:
                continue
            before = clause[: match.start()]
            after = clause[match.end() :]
            if _CM_AFTER.match(after):
                measures.append(value)
                continue
            if _ID_BEFORE.search(before) or len(raw.replace(",", "").split(".")[0]) > _LONGEST_AMOUNT_DIGITS:
                continue
            if _NOT_MONEY_AFTER.match(after) or value < _SMALLEST_AMOUNT:
                continue
            if money_clause or _PRICE_BEFORE.search(before):
                money.append(value)
    return money, measures


def ungrounded(text: str, history: list[dict], constants: set[Decimal] | None = None) -> list[str]:
    """The money amounts and measurements in `text` nothing in the
    conversation said, formatted for a log line and a nudge. Empty is clean."""
    money, measures = stated(text)
    if not (money or measures):
        return []
    known = known_numbers(history, constants)
    missing: list[str] = []
    for value in money:
        if value not in known and f"{value:f} جنيه" not in missing:
            missing.append(f"{value:f} جنيه")
    for value in measures:
        if value not in known and f"{value:f} سم" not in missing:
            missing.append(f"{value:f} سم")
    return missing


#: The garment-flat caveat. `AGENTS.md`: the numbers are measurements of the
#: garment laid flat, not of a body -- "say which, every time". The prompt
#: asked; a customer who reads a 56 cm width as a chest measurement orders two
#: sizes too small, so it is appended here whenever a reply quotes one and
#: does not already say so.
FLAT_NOTE = "المقاسات دي للقطعة وهي مفرودة، مش مقاسات الجسم."

_ALREADY_FLAT = re.compile(r"مفرود|garment|laid flat|\bflat\b", re.I)


def with_flat_note(text: str) -> str:
    """`text`, with the garment-flat caveat added if it quotes centimetres
    and does not already carry it."""
    _money, measures = stated(text)
    if not measures or _ALREADY_FLAT.search(text or ""):
        return text
    return f"{text.rstrip()}\n{FLAT_NOTE}"
