"""The fixed answers a public Instagram comment can get without a model.

Some comments ask a question whose answer is the same for every customer and
every product -- how long delivery takes, what shipping costs, whether the
shop takes cash. Before this module those classified as `important` and each
one cost a DM, when one public sentence answers them completely, in the place
the question was asked, for everyone else scrolling past to read too.

**This is a lookup, not a classifier. There is no model call here, ever.**
The public surface must never display a sentence a model chose, which is the
same rule the fixed `PUBLIC_ACKS` follow and the same one
`domain/services/search_terms.py` states in its own docstring: when the
answer is a rule, it lives below the model, where it is a rule.

Matching runs over `search_terms.normalize` -- the catalog search's own
folding, so `التوصيل`/`التوصيل` with diacritics, `أ`/`ا`, `ى`/`ي` and `ة`/`ه`
are one spelling and the franco half is lowercased and stripped of
punctuation. Each key then needs *two* things present, never one: a subject
(shipping, payment) and the question being asked about it. `الشحن بكام` is a
shipping price; `الشحن كام يوم` is a delivery time; `بكام ده` is neither, and
must reach the model as a product question.
"""

from __future__ import annotations

import re

from domain.services.search_terms import expand, normalize

#: The exact public replies. Verbatim strings, not templates: every one of
#: these is published under a post where anyone can read it.
#:
#: The 110 EGP is hardcoded on purpose -- shipping is one flat rate to every
#: governorate, confirmed across ~100 completed orders, so there is nothing to
#: look up per customer. The live fee the bot quotes *in DM* comes from the
#: `ShippingRate` table (`domain/services/shipping.py::get_fee`), not from
#: Shopify; if that flat rate ever changes, this string changes with it.
#:
#: No URL in the payment line: Instagram suppresses the reach of a comment
#: carrying a link, so the answer would be published and then unread.
FAQ_REPLIES: dict[str, str] = {
    "delivery_time": "التوصيل بياخد لغاية 4 أيام لكل محافظات مصر 🖤",
    "shipping_cost": "الشحن 110 جنيه لكل محافظات مصر 🖤",
    "payment": "بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع 🖤",
}

#: Written already normalized (no hamza, `ي` not `ى`, `ه` not `ة`, lowercase)
#: because that is what `normalize` produces on the way in. Arabic and franco
#: for each: a good half of Egyptian comments are typed in latin letters.
_SUBJECT_SHIPPING = re.compile(
    r"(?:ال)?(?:توصيل|شحن|شحنه|دليفري|ديليفري|اوردر|الطلب)"
    r"|\b(?:delivery|deliver|shipping|shipment|ship|tawsil|tawseel|taw9eel|shahn|order)\b"
)

_ASKS_HOW_LONG = re.compile(
    r"(?:قد ايه|كام يوم|كام ايام|في يوم|امتي|امته|بياخد|هياخد|بياخذ|ياخد|بتاخد|هتاخد"
    r"|مده|المده|بيوصل|هيوصل|يوصل|بيجي|هيجي)"
    r"|\b(?:how long|when|days|kam yom|kam youm|ad eh|2ad eh|emta|amta|byakhod|byakhud"
    r"|hayakhod|yewsal|yousal|takes|take)\b"
)

_ASKS_HOW_MUCH = re.compile(
    r"(?:بكام|كام|سعره|سعر|السعر|تكلفه|التكلفه|تمن|التمن|بيتكلف|بكم)"
    r"|\b(?:how much|price|cost|costs|fees|fee|bkam|bekam|b kam|kam|se3r|ser3|presso)\b"
)

_PAYMENT = re.compile(
    r"(?:كاش|الدفع|ادفع|بدفع|تدفع|ندفع|بتقبلوا|بتاخدوا|بتقبل|فيزا|فيزه|انستاباي"
    r"|فودافون كاش|محفظه|عند الاستلام|الاستلام|كاش عند)"
    r"|\b(?:cash|cod|visa|payment|pay|paying|instapay|vodafone cash|wallet|credit card)\b"
)

#: Each key: every group must match somewhere in the normalized text. Order
#: matters -- `الشحن كام يوم` satisfies both the time and the price group, and
#: it is a delivery-time question, so that key is tested first.
_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    ("delivery_time", (_SUBJECT_SHIPPING, _ASKS_HOW_LONG)),
    ("shipping_cost", (_SUBJECT_SHIPPING, _ASKS_HOW_MUCH)),
    ("payment", (_PAYMENT,)),
)

#: Keys whose answer stops being fixed the moment a product is named. "الشحن
#: بكام" is a flat rate; "الهودي الأسود ده بكام؟" is a product question with a
#: price the shop has to look up, and answering it from a table here would
#: publish a wrong number under a post.
_PRODUCT_SENSITIVE = {"shipping_cost"}


def _names_a_product(normalized: str) -> bool:
    """Whether any word here is catalog vocabulary -- a garment, a colour, a
    fit.

    Asked through the search's own `expand`, rather than against a second
    list kept here: a word added to the search vocabulary is then a word this
    respects too, with no edit in this file. `expand` returns the token plus
    whatever the catalog calls it, so anything longer than the token itself
    is a word the shop sells things by."""
    return any(len(expand(token)) > 1 for token in normalized.split())


def match(text: str) -> str | None:
    """The FAQ key this comment asks for, or None to let the classifier see it."""
    normalized = normalize(text)
    if not normalized:
        return None
    for key, groups in _RULES:
        if not all(group.search(normalized) for group in groups):
            continue
        if key in _PRODUCT_SENSITIVE and _names_a_product(normalized):
            return None
        return key
    return None


def reply_for(key: str) -> str:
    return FAQ_REPLIES[key]
