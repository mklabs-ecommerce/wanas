"""What a reply says about sleeve length, checked against what was recorded.

Production, 2026-09-25: «بس ده كم طويل مش نص كم» about a short-sleeved tee,
to a customer who had not mentioned sleeves, and «كم طويل» about the same
tee in four more replies. The words came from a tool result -- the catalog
had inferred "long" from the category -- so no check that compares a reply
with its tool results could have caught it. That half is fixed at the source
(`domain/services/sleeves.py`: an unclassified product has no sleeve). This
is the other half: the reply's claim, held to the record.

A claim is a sleeve phrase the reply *asserts* -- «مش نص كم» and «ولا نص كم»
deny rather than assert. It is about the products the reply names (by the
names the tools returned), or, naming none, the product the conversation is
about. It must be true of at least one of them:

* a claim about a product whose sleeve is recorded otherwise is wrong;
* a claim about a product with no sleeve recorded is a guess -- the answer
  there is "I'm not sure", and the photo.

And the old rule, kept but narrowed: professing not to know a sleeve length
that *is* recorded for the product in question is refusing to read the tool
result. Saying so about one that is not recorded is simply true.
"""

from __future__ import annotations

import re

from assistant.messages import TOOL_RESULTS

#: Assertions of each value, as a reply writes them.
_PHRASES: dict[str, tuple[str, ...]] = {
    "half": ("نص كم", "نُص كم", "نصف كم", "كم قصير", "half sleeve", "short sleeve"),
    "long": ("كم طويل", "long sleeve"),
    "sleeveless": ("من غير كم", "بدون كم", "sleeveless"),
}

#: A word straight before the phrase that turns it into a denial.
_NEGATION = re.compile(r"(?:مش|مو|ولا|مفيش|مافيش|not|no)\s*(?:بـ?|ب)?\s*$", re.IGNORECASE)

#: "I don't know" beside a sleeve word (`reply_rules._NO_DATA`, narrowed).
_UNKNOWN = re.compile(
    r"مش متأكد|مش متاكد|مش عارف|مش متسج|مش مسج|غير مسجل|معنديش|مفيش معلومات|مفيش بيانات"
    r"|not sure|not recorded|don't know",
    re.IGNORECASE,
)
_ABOUT_SLEEVES = re.compile(r"الكم|كم طويل|نص كم|كم قصير|sleeve", re.IGNORECASE)


def asserted(text: str) -> set[str]:
    """The sleeve values `text` asserts, denials excluded."""
    lowered = (text or "").casefold()
    found: set[str] = set()
    for value, phrases in _PHRASES.items():
        for phrase in phrases:
            for match in re.finditer(re.escape(phrase.casefold()), lowered):
                if not _NEGATION.search(lowered[max(0, match.start() - 12) : match.start()]):
                    found.add(value)
    return found


def recorded(history: list[dict]) -> dict[str, str | None]:
    """`{product_id: sleeve or None}` for every product a tool in this
    conversation described, newest answer winning."""
    found: dict[str, str | None] = {}
    for message in history:
        if message.get("role") != TOOL_RESULTS:
            continue
        for result in message.get("results") or []:
            content = result.get("content")
            if not isinstance(content, dict):
                continue
            entries = [content] + [e for e in content.get("products") or [] if isinstance(e, dict)]
            for entry in entries:
                product_id = entry.get("product_id")
                if isinstance(product_id, str) and "sleeve" in entry:
                    found[product_id] = entry.get("sleeve") or None
    return found


def _in_scope(text: str, history: list[dict]) -> list[str]:
    from assistant import showcase
    from assistant.tools.base import last_product

    named = [pid for pid, *_ in showcase.named_products(text, showcase.known_products(history))]
    if named:
        return named
    current = last_product(history)
    return [current["product_id"]] if current and current.get("product_id") else []


def problem(text: str, history: list[dict]) -> str:
    """What is wrong with the reply's sleeve claims, or ""."""
    claims = asserted(text)
    unknown = bool(_UNKNOWN.search(text or "")) and bool(_ABOUT_SLEEVES.search(text or ""))
    if not claims and not unknown:
        return ""
    scope = _in_scope(text, history)
    if not scope:
        return ""
    record = recorded(history)
    values = {record[pid] for pid in scope if pid in record}
    if not values:
        return ""  # nothing looked up about these products: not this rule's call
    for claim in sorted(claims):
        if claim not in values:
            if values == {None}:
                return f"stated {claim!r} about a product with no sleeve length recorded"
            recorded_values = ", ".join(sorted(v or "not recorded" for v in values))
            return f"stated {claim!r} about a product recorded as {recorded_values}"
    if unknown and not claims and None not in values:
        return "said it does not know a sleeve length the tool result records"
    return ""
