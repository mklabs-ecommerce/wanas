"""Sleeve length: the one garment fact the catalog could not state.

A customer asked for «البولو النص كم» -- the half-sleeve polo -- and the bot
answered that the shop has two polos but "no published data about sleeve
length for either", and offered to fetch a person. Two holes behind that: the
phrase was not in the catalog's vocabulary (that lives in
`domain.services.search_terms`), and nothing recorded sleeve length at all.

This module is the second half, and it has one rule: **every product has a
definite answer.** There is no "not recorded".

That is a correction. The first version of this field left a product unset
when nobody had said, on the reasoning that a guess is worse than a shrug --
and it made the bot answer "I don't have that information" for every product
outside the half-sleeve list, which is worse than the bug it replaced. A
customer asking whether a hoodie is half-sleeve is not helped by being told
the shop does not know what it sells.

So the half-sleeve list is **closed** -- those products and no others -- and
everything else takes the value its garment type implies. A product that
reaches here with nothing set (a new one mirrored from Shopify, which has no
such field) is not unknown either: `for_category` answers for it, always with
something that is *not* half, so the worst case is a hoodie described as
long-sleeved rather than a shop that cannot describe its own stock.
"""

from __future__ import annotations

#: The three values `Product.sleeve` may hold, plus `None` for "not recorded".
HALF = "half"
LONG = "long"
SLEEVELESS = "sleeveless"

SLEEVES: tuple[str, ...] = (HALF, LONG, SLEEVELESS)

#: What goes into the free-text haystack `catalog._haystack` builds, so a
#: search for the words below finds the product. Both English spellings of
#: the half case are written out because customers and the model use both,
#: and `search_terms` matches a token against text rather than against a
#: canonical value.
SEARCH_TEXT: dict[str, str] = {
    HALF: "half sleeve short sleeve",
    LONG: "long sleeve",
    SLEEVELESS: "sleeveless no sleeve",
}

#: How a reply may describe each value, in the shop's own Arabic. Used by the
#: tool layer so the model is handed the phrase rather than translating a bare
#: English enum itself.
LABELS_AR: dict[str, str] = {
    HALF: "نص كم",
    LONG: "كم طويل",
    SLEEVELESS: "من غير كم",
}

#: Everything a caller might hand in meaning one of the three. Staff type into
#: a dashboard field, the model passes a filter argument it chose the wording
#: of, and a seed file is written by a person -- so "short", "short sleeve",
#: "half-sleeve" and «نص كم» all have to land on the same value rather than on
#: a fourth one nothing else recognises.
_SPELLINGS: dict[str, tuple[str, ...]] = {
    HALF: (
        "half", "half sleeve", "half-sleeve", "halfsleeve", "half sleeves",
        "short", "short sleeve", "short-sleeve", "shortsleeve", "short sleeves",
        "نص كم", "نصكم", "نصف كم", "كم قصير", "هاف", "هاف سليف",
    ),
    LONG: (
        "long", "long sleeve", "long-sleeve", "longsleeve", "long sleeves",
        "full", "full sleeve", "كم طويل", "كم كامل", "لونج", "لونج سليف",
    ),
    SLEEVELESS: (
        "sleeveless", "no sleeve", "no sleeves", "none",
        "بدون كم", "من غير كم", "سليفلس",
    ),
}

_ALIASES: dict[str, str] = {
    spelling: canonical
    for canonical, spellings in _SPELLINGS.items()
    for spelling in spellings
}


#: What a garment of each kind is, when nobody has said otherwise. Every entry
#: is deliberately **not** `HALF`: the half-sleeve list is closed, so a
#: product nobody has classified cannot be on it, and the one thing this
#: default must never do is add a product to an answer about half sleeves.
#:
#: Trousers take `SLEEVELESS` because that is literally true -- they have no
#: sleeves -- and because "is this half sleeve?" about a sweatpant deserves
#: "it's trousers" rather than "no".
_BY_CATEGORY: dict[str, str] = {
    "hoodies & sweatshirts": LONG,
    "jackets": LONG,
    "polo shirts": LONG,
    "joggers & sweatpants": SLEEVELESS,
    "t-shirts": LONG,
    "tops": LONG,
}

#: For a category this shop has never had. Long rather than half, for the same
#: reason every entry above is: a closed list is only closed if nothing can
#: fall into it by accident.
FALLBACK = LONG


def for_category(category) -> str:
    """The sleeve length a product of this kind has, absent anything better.

    Used for a product mirrored in from Shopify -- which has no sleeve field,
    so there is nothing to mirror -- and as the last line of defence on the
    read path, so a NULL that somehow survives is still answered with a
    sentence rather than a shrug.
    """
    key = " ".join(str(category or "").split()).lower()
    return _BY_CATEGORY.get(key, FALLBACK)


def effective(sleeve, category) -> str:
    """What to *tell a customer*, which is never "we don't know".

    The column is still nullable -- a database this code did not write is
    allowed to exist -- but nothing above this line ever sees a None. That is
    what makes "unset" unreachable in an answer rather than merely unlikely.
    """
    return normalise(sleeve) or for_category(category)


def normalise(value) -> str | None:
    """One of `SLEEVES`, or None when the input says nothing.

    None here means "the caller passed nothing", not "the product is a
    mystery" -- `effective` above is what turns it into an answer. Anything
    unrecognised answers None too rather than being stored as itself: a fourth
    value in this column would be a sleeve length no search, no filter and no
    reply knows how to read.
    """
    if not isinstance(value, str):
        return None
    key = " ".join(value.strip().lower().split())
    if not key:
        return None
    return _ALIASES.get(key) or (key if key in SLEEVES else None)
