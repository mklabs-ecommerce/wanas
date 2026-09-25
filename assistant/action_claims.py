"""What a reply says it *did*, checked against what the turn's tools did.

The third of the same family: `photo_claims` checks the words against the
pictures, `order_change_claims` against the staff-queue item, and this one
against the two actions a shop conversation turns on -- putting something in
the cart and placing the order. The words are the model's; whether
`add_to_cart` accepted the variant and whether `confirm_order` returned an
order are the tool layer's; and nothing joined the two before the reply left.
So «ضفتهولك في السلة» could go out beside an `out_of_stock` refusal, or beside
no call at all, and «الأوردر اتسجل» beside a cart nobody had checked out.

Only a claim in the **past tense** counts -- «ضفته», «اتضاف», «اتسجل الأوردر»:
an offer («أضيفه؟») or a plan («هضيفه») promises nothing yet. A negated one
(«لسه مضفتوش», «مقدرتش أضيفه») is the honest version and is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_TASHKEEL = re.compile(r"[ً-ْـٰ]")
_CLAUSE = re.compile(r"[\n.،؛!؟?]+")

#: Something was put in the cart. Past tense only, and never after the
#: negating «م» Egyptian glues to the verb («مضفتش»).
_ADDED = re.compile(
    r"(?<![مa-z])(?:ضفت|ضفنا|ضفتل|اتضاف|اتضافت|اتضفت|اتحط|اتحطت|حطيت|حطينا|حطتهولك)"
    r"|\badded (?:it |them |that )?to (?:your |the )?(?:cart|bag|basket)\b",
    re.I,
)

#: The order was placed. Past or passive, about *the* order.
_PLACED = re.compile(
    r"(?:الأوردر|الاوردر|أوردرك|اوردرك|طلبك|الطلب)\s*(?:اتأكد|اتاكد|اتسجل|اتعمل|اتنفذ|اتبعت|اتأكدت)"
    r"|(?:اتأكد|اتاكد|اتسجل|اتعمل|تم تأكيد|تم تسجيل)\s*(?:الأوردر|الاوردر|أوردرك|اوردرك|طلبك|الطلب)"
    r"|\border (?:is |has been |was )?(?:confirmed|placed)\b",
    re.I,
)

#: The honest versions of both: not done, could not, not yet.
_NEGATED = re.compile(r"مش|مقدرتش|ماقدرتش|لسه|للأسف|مااتضاف|ماتضاف|مش هقدر|not |n't|couldn't")

#: A turn that looked up the customer's existing orders is talking about
#: those, and «أوردرك اتأكد» there is a status, not a claim about this cart.
_ORDER_STATUS_TOOLS = {"get_my_orders", "get_return_terms", "cancel_order"}


def _claims(pattern: re.Pattern, text: str) -> bool:
    plain = _TASHKEEL.sub("", text or "")
    return any(
        pattern.search(clause) and not _NEGATED.search(clause) for clause in _CLAUSE.split(plain)
    )


def _succeeded(results: list[tuple[str, dict]], *names: str) -> bool:
    return any(
        name in names and isinstance(content, dict) and "error" not in content
        for name, content in results
    )


def unbacked(
    text: str,
    results: list[tuple[str, dict]],
    *,
    cart_has_items: Callable[[], bool],
    has_orders: Callable[[], bool],
) -> str:
    """"cart" or "order" when the reply claims that action and nothing this
    turn did it; "" when the claim is backed or there is none.

    `results` are this turn's `(tool name, result)` pairs, in order. The two
    questions about the database are asked only when a claim needs them.
    """
    called = {name for name, _content in results}
    if _claims(_ADDED, text) and not _succeeded(
        results, "add_to_cart", "add_item_to_order", "request_item_add"
    ):
        return "cart"
    if (
        _claims(_PLACED, text)
        and not _succeeded(results, "confirm_order")
        and not called & _ORDER_STATUS_TOOLS
        # `confirm_order` empties the cart and writes an order: a cart still
        # full, or a customer with no order at all, is an order not placed.
        and (cart_has_items() or not has_orders())
    ):
        return "order"
    return ""


#: Said instead, when the model will not stop claiming it. Each is true of the
#: turn it replaces, and asks for the one thing that lets the next one do it.
FALLBACKS = {
    "cart": "معلش، القطعة لسه مااتضافتش للسلة. قولي المقاس واللون اللي عايزهم وأضيفها على طول.",
    "order": "الأوردر لسه ماتسجلش. أول ما تأكدلي البيانات هسجله على طول.",
}
