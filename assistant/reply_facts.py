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
* a **measurement** is a number directly followed by «سم» / «cm»;
* a **duration** is a number of days or hours -- «4 أيام», «من 2 لـ 4 أيام»,
  «خلال 24 ساعة». The shop publishes one delivery promise («بياخد لغاية 4
  أيام», `shop_facts.DELIVERY_DAYS`) and one exchange window; «من 2 لـ 4
  أيام» was quoted to customers on 2026-09-22 and 09-25, and the «2» was the
  prompt's own layout example, not anything the shop ever promised;
* a **count of what is left** -- «باقي حاجة واحدة», «آخر قطعة», «فاضل
  قطعتين» -- has to be a `stock_qty` a tool returned, not merely a number
  that appears somewhere: on 2026-09-25 «باقي حاجة واحدة لكل لون» went out
  beside a get_variants answer of two each, and a «1» was sitting in the
  same conversation as a search's `count`.

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
    """The numbers the shop publishes without a lookup: its shipping fees, the
    exchange surcharge and window, and the delivery promise. Read from where
    they are kept, never restated."""
    from domain.models import ShippingRate
    from domain.services.orders import EXCHANGE_SURCHARGE, EXCHANGE_WINDOW_HOURS
    from domain.services.shop_facts import DELIVERY_DAYS

    found = {
        _decimal(str(EXCHANGE_SURCHARGE)),
        _decimal(str(EXCHANGE_WINDOW_HOURS)),
        _decimal(str(DELIVERY_DAYS)),
    }
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


#: A number of days or hours, or a range of them: «4 أيام», «من 2 لـ 4
#: أيام», «2-4 days», «خلال 24 ساعة».
_DURATION = re.compile(
    r"(\d+)\s*(?:(?:لـ|ل|الى|إلى|لغاية|to|-|–)\s*(\d+)\s*)?"
    r"(?:يوم|ايام|أيام|أيّام|ساعة|ساعه|ساعات|days?|hours?)",
    re.IGNORECASE,
)
_TWO_DAYS = re.compile(r"(?<![\u0600-\u06ff])يومين(?![\u0600-\u06ff])")


def durations(text: str) -> list[Decimal]:
    """The day and hour counts a reply states."""
    folded = (text or "").translate(_ARABIC_INDIC)
    found: list[Decimal] = []
    for match in _DURATION.finditer(folded):
        for raw in match.groups():
            value = _decimal(raw) if raw else None
            if value is not None and value not in found:
                found.append(value)
    if _TWO_DAYS.search(folded) and Decimal(2) not in found:
        found.append(Decimal(2))
    return found


_COUNT_WORDS = {
    "واحد": 1, "واحدة": 1, "واحده": 1, "قطعة": 1, "قطعه": 1, "حتة": 1, "حته": 1,
    "اتنين": 2, "اثنين": 2, "قطعتين": 2, "حتتين": 2, "عددين": 2,
    "تلاتة": 3, "تلاته": 3, "ثلاثة": 3, "تلات": 3,
}

#: What is left of something: «باقي حاجة واحدة», «فاضل منه 3», «متبقي منه
#: عددين», «آخر قطعة».
_LEFT = re.compile(
    r"(?:باقي|باقى|فاضل|فاضله|فاضلة|متبقي|متبقى|فضل|فضلت|آخر|اخر)\s+"
    r"(?:(?:منه|منها|منهم)\s+)?(?:(?:حاجة|حاجه|قطعة|قطعه|حتة|حته)\s+)?"
    r"(\d+|" + "|".join(sorted(_COUNT_WORDS, key=len, reverse=True)) + r")"
    r"(?![\u0600-\u06ff])"
)


def stock_counts(text: str) -> list[int]:
    """The counts of remaining stock a reply states."""
    folded = (text or "").translate(_ARABIC_INDIC)
    found: list[int] = []
    for match in _LEFT.finditer(folded):
        raw = match.group(1)
        value = int(raw) if raw.isdigit() else _COUNT_WORDS.get(raw)
        if value is not None and value not in found:
            found.append(value)
    return found


def _stock_values(value, out: set[int]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "stock_qty" and isinstance(item, int) and not isinstance(item, bool):
                out.add(item)
            else:
                _stock_values(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _stock_values(item, out)


def known_stock(history: list[dict]) -> set[int]:
    """Every `stock_qty` a tool in this conversation returned, plus whatever
    numbers the customer typed."""
    found: set[int] = set()
    for message in history:
        role = message.get("role")
        if role == TOOL_RESULTS:
            for result in message.get("results") or []:
                _stock_values(result.get("content"), found)
        elif role == USER:
            numbers: set[Decimal] = set()
            _numbers_in(message.get("content") or "", numbers)
            found.update(int(n) for n in numbers if n == n.to_integral_value())
    return found


def ungrounded(text: str, history: list[dict], constants: set[Decimal] | None = None) -> list[str]:
    """The money amounts, measurements, durations and stock counts in `text`
    nothing in the conversation said, formatted for a log line and a nudge.
    Empty is clean."""
    money, measures = stated(text)
    spans = durations(text)
    left = stock_counts(text)
    if not (money or measures or spans or left):
        return []
    if left:
        stock = known_stock(history)
        missing_stock = [f"{n} قطعة" for n in left if n not in stock]
    else:
        missing_stock = []
    known = known_numbers(history, constants)
    missing: list[str] = []
    for value in money:
        if value not in known and f"{value:f} جنيه" not in missing:
            missing.append(f"{value:f} جنيه")
    for value in measures:
        if value not in known and f"{value:f} سم" not in missing:
            missing.append(f"{value:f} سم")
    for value in spans:
        if value not in known and f"{value:f} يوم/ساعة" not in missing:
            missing.append(f"{value:f} يوم/ساعة")
    return missing + missing_stock


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
