"""Sleeve length: the one garment fact the catalog could not state.

A customer asked for «البولو النص كم» -- the half-sleeve polo -- and the bot
answered that the shop has two polos but "no published data about sleeve
length for either", and offered to fetch a person. Two separate holes, and
this module closes the second of them:

* the words. «نص كم» is not in the catalog's vocabulary, and the catalog is
  written in English, so the phrase matched nothing. That lives in
  `domain.services.search_terms`, which maps the Arabic and franco spellings
  onto the tokens below.
* the fact. Nothing anywhere recorded whether a garment is half-sleeve,
  long-sleeve or sleeveless, so even a perfectly understood question had no
  answer behind it. Shopify has no field for it -- exactly like `style`,
  `department` and `collection` -- so it is a wanas.db column,
  `Product.sleeve`, and this module is its vocabulary.

`None` is a real and correct value: "nobody has recorded it for this product".
It is not "no sleeves", and it is not an invitation to guess from the
category. A hoodie is long-sleeved in almost every shop on earth and is still
not long-sleeved *here* until somebody says so, because the alternative is the
bot inventing a garment fact -- which is the failure this whole codebase is
built against.
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


def normalise(value) -> str | None:
    """One of `SLEEVES`, or None for "not recorded".

    Anything unrecognised also answers None rather than being stored as
    itself. A fourth value in this column would be a sleeve length no search,
    no filter and no reply knows how to read -- silently invisible, which is
    the shape of the bug this module exists to fix.
    """
    if not isinstance(value, str):
        return None
    key = " ".join(value.strip().lower().split())
    if not key:
        return None
    return _ALIASES.get(key) or (key if key in SLEEVES else None)
