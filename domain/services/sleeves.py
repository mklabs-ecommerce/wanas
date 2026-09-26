"""Sleeve length: a garment fact the catalog records, or does not.

A customer asked for «البولو النص كم» -- the half-sleeve polo -- and the bot
answered that the shop has two polos but "no published data about sleeve
length for either", and offered to fetch a person. Two holes behind that: the
phrase was not in the catalog's vocabulary (that lives in
`domain.services.search_terms`), and nothing recorded sleeve length at all.
Every product in the seed now records one, and the dashboard asks for it.

**A product nobody classified has no sleeve length, and says so.** An earlier
version of this module answered for it from its category -- "T-Shirts ->
long" -- on the reasoning that a shrug is worse than a guess. Production,
2026-09-25: a short-sleeved plain tee created in the dashboard (whose form
also preselected «كم طويل») was described as «كم طويل» in five replies across
two channels, one of them to a customer who never mentioned sleeves, and the
prompt forbade the bot from doubting it. A guess stated as a fact is the
failure this shop cannot afford; "I'm not sure, here is the photo" is not.
So `recorded` answers None for an unclassified product, the tools leave the
field out, and the bot is told what that means.
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


def recorded(sleeve) -> str | None:
    """What may be *told to a customer*: one of `SLEEVES`, or None when nobody
    has recorded it. Never inferred from the category -- see the module
    docstring for what inferring it cost."""
    return normalise(sleeve)


def normalise(value) -> str | None:
    """One of `SLEEVES`, or None when the input says nothing.

    Anything unrecognised answers None too rather than being stored as itself:
    a fourth value in this column would be a sleeve length no search, no
    filter and no reply knows how to read.
    """
    if not isinstance(value, str):
        return None
    key = " ".join(value.strip().lower().split())
    if not key:
        return None
    return _ALIASES.get(key) or (key if key in SLEEVES else None)
