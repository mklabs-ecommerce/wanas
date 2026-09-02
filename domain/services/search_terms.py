"""Arabic and franco-Arabic vocabulary for the catalog search.

The catalog is written in English -- `Boxy WNS Tee`, `T-Shirts`, `Olive`,
`boxy-fit` -- and the customers are not. `get_products(query="هودي أسود")`
matched nothing at all before this module existed; the bot only appeared to
work because the model happened to translate before calling the tool. That is
a habit, not a guarantee, and the first time it does not translate the shop
tells a customer "we don't have that" about something on the shelf.

So the translation happens *here*, below the model, where it is a rule:

* `normalize` folds the spellings that mean the same thing. Egyptians type
  `هودي` / `هودى`, `زيتي` / `زيتى`, `اسود` / `أسود`, with and without
  diacritics, and a plain substring match treats each as a different word.
* `SYNONYMS` maps a normalized Arabic or franco token onto the English
  token(s) that actually appear in the catalog.
* `STOPWORDS` drops the words a request is padded with -- «عايز», «لو سمحت»,
  «بكام» -- which otherwise fail the all-tokens-must-match rule and turn a
  perfectly good query into zero results.

Adding a word is a one-line change here and needs no other file.
"""

from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

#: Harakat, tanwin, shadda, sukun and the tatweel stretch character. All
#: decorative: `أَسْوَد` and `أسود` are the same word to everyone except a
#: substring match.
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")

#: The letters that carry more than one accepted spelling in everyday typing.
_LETTER_FOLD = str.maketrans(
    {
        "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
        "ى": "ي", "ئ": "ي",
        "ؤ": "و",
        "ة": "ه",
        "ﻻ": "لا",
        # Arabic-Indic digits, which phone keyboards produce by default.
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    }
)

#: Anything that is not a letter or a digit becomes a space. Keeps `quarter-zip`
#: and `quarter zip` from being different searches.
_NON_WORD = re.compile(r"[^\w؀-ۿ]+", re.UNICODE)


def normalize(text: str) -> str:
    """Fold one string into the form both sides of a match are compared in."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).lower()
    folded = _DIACRITICS.sub("", folded)
    folded = folded.translate(_LETTER_FOLD)
    folded = _NON_WORD.sub(" ", folded)
    return " ".join(folded.split())


def _strip_article(token: str) -> str:
    """`الهودي` -> `هودي`.

    Only when something is left over: `ال` on its own is not an article, and
    stripping a three-letter word down to one letter matches everything.
    """
    if len(token) > 3 and token.startswith("ال"):
        return token[2:]
    return token


# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

#: Normalized Arabic/franco token -> the English tokens it should match.
#: Written as normalized forms already (no hamza, `ي` not `ى`, `ه` not `ة`)
#: because that is what `normalize` produces on the way in.
_RAW_SYNONYMS: dict[str, tuple[str, ...]] = {
    # --- garments -------------------------------------------------------
    "تيشيرت": ("tee", "t-shirts", "t shirt", "tshirt"),
    "تيشرت": ("tee", "t-shirts", "t shirt", "tshirt"),
    "تشيرت": ("tee", "t-shirts", "t shirt", "tshirt"),
    "تيشيرتات": ("tee", "t-shirts"),
    "قميص": ("tee", "polo", "shirts"),
    "tshirt": ("tee", "t-shirts"),
    "tee": ("tee", "t-shirts"),
    "هودي": ("hoodie", "hoodies"),
    "هوديز": ("hoodie", "hoodies"),
    "هوود": ("hoodie",),
    "hoodi": ("hoodie",),
    "hodie": ("hoodie",),
    "hodi": ("hoodie",),
    "سويت": ("sweatshirts", "sweatpant"),
    "سويتشيرت": ("sweatshirts", "crewneck"),
    "سويت شيرت": ("sweatshirts", "crewneck"),
    "swetshirt": ("sweatshirts",),
    "بولو": ("polo",),
    "polo": ("polo",),
    "بنطلون": ("sweatpant", "joggers"),
    "بنطالون": ("sweatpant", "joggers"),
    "بنطلونات": ("sweatpant", "joggers"),
    "باتنطلون": ("sweatpant",),
    "تراك": ("sweatpant", "joggers"),
    "جوجر": ("joggers", "sweatpant"),
    "جوجرز": ("joggers", "sweatpant"),
    "سويت بانت": ("sweatpant",),
    "سويت بانتس": ("sweatpant", "joggers"),
    "بانتس": ("sweatpant", "joggers"),
    "pantalon": ("sweatpant",),
    "جاكيت": ("jacket", "jackets"),
    "جاكت": ("jacket", "jackets"),
    "چاكيت": ("jacket", "jackets"),
    "jaket": ("jacket",),
    "توب": ("top", "tops"),
    "توبات": ("top", "tops"),
    "بلوزه": ("top", "tops"),
    "كروب": ("top", "tops"),
    "زيب": ("zip", "zipup", "zip-through"),
    "زيبب": ("zipup",),
    "سوستة": ("zip", "zipup", "zip-through"),
    "سوسته": ("zip", "zipup", "zip-through"),
    "نص سوسته": ("quarter-zip",),
    "كاروهات": ("knitted",),
    "تريكو": ("knitted",),
    "كنزه": ("crewneck", "sweatshirts"),
    # --- colours ---------------------------------------------------------
    "اسود": ("black",),
    "سوده": ("black",),
    "بلاك": ("black",),
    "eswed": ("black",),
    "ابيض": ("white",),
    "بيضه": ("white",),
    "وايت": ("white",),
    "abyad": ("white",),
    "رمادي": ("grey", "gray"),
    "رصاصي": ("grey", "gray"),
    "جراي": ("grey", "gray"),
    "grey": ("grey", "gray"),
    "gray": ("grey", "gray"),
    "زيتي": ("olive",),
    "اوليف": ("olive",),
    "زيتوني": ("olive",),
    "كحلي": ("navy",),
    "نيفي": ("navy",),
    "ازرق غامق": ("navy",),
    "بيج": ("beige",),
    "بني": ("brown",),
    "بنى": ("brown",),
    "كافيه": ("brown", "camel brown"),
    "جملي": ("camel brown", "brown"),
    "نبيتي": ("burgundy",),
    "خمري": ("burgundy",),
    "بوردو": ("burgundy",),
    "وردي": ("pink",),
    "بينك": ("pink",),
    "زهري": ("pink",),
    "اخضر": ("vintage green", "green", "olive"),
    "اخضر فاتح": ("vintage green",),
    "بمبي": ("pink",),
    # --- fit / style -----------------------------------------------------
    "واسع": ("oversized", "boxy-fit", "wide-leg"),
    "وايد": ("wide-leg",),
    "اوفر": ("oversized",),
    "اوفرسايز": ("oversized",),
    "لوز": ("oversized",),
    "ضيق": ("fitted",),
    "فيتد": ("fitted",),
    "بوكسي": ("boxy-fit",),
    "خفيف": ("lightweight",),
    "صيفي": ("tee", "top", "lightweight"),
    "شتوي": ("hoodie", "jacket", "sweatshirts"),
    "مطبوع": ("graphic",),
    "رسمه": ("graphic",),
    "برسمه": ("graphic",),
    # --- department ------------------------------------------------------
    "حريمي": ("women",),
    "بناتي": ("women",),
    "ستاتي": ("women",),
    "رجالي": ("unisex",),
    "يونيسكس": ("unisex",),
    # --- collections -----------------------------------------------------
    "كايروكي": ("cairokee",),
    "الكايروكي": ("cairokee",),
    "كيروكي": ("cairokee",),
    "شتوية": ("winter",),
    "وناس": ("wanas", "wns"),
    # --- product names ---------------------------------------------------
    # The class this table was missing. Everything above translates a word
    # that describes a garment -- its kind, its colour, its cut -- and those
    # were written down because the catalog holds no Arabic. A product *name*
    # is English too, and a customer says it in Arabic letters exactly the
    # same way: «عايز الرينجر تيشيرت». Only the two names that double as
    # collections (`cairokee`, `wanas`) were ever listed, so every other
    # product was reachable in Arabic only by its category -- and «رينجر»
    # returned nothing at all about a tee that is on the shelf, which is the
    # wrong answer this module exists to prevent. Names, not descriptions,
    # so a new product with an English name needs a line here.
    "رينجر": ("ringer",),
    "رنجر": ("ringer",),
    "انفي": ("envy",),
    "اينفي": ("envy",),
    "ووركر": ("worker",),
    "وركر": ("worker",),
    "هارت": ("heart",),
    "قلب": ("heart",),
    "فيلين": ("feelin",),
    "فيلين فاين": ("feelin fine",),
    "كرو نك": ("crewneck",),
    "كرونيك": ("crewneck",),
    "كرونك": ("crewneck",),
    "كوارتر": ("quarter-zip",),
    "كوارتر زيب": ("quarter-zip",),
    "ربع سوسته": ("quarter-zip",),
    "زيب اب": ("zipup",),
    "زيباب": ("zipup",),
    "زيب هودي": ("zip-through", "zipup"),
}

#: Padding. Every one of these appears in a real request and none of them is
#: a product attribute, so they are dropped before the all-tokens rule runs.
STOPWORDS: frozenset[str] = frozenset(
    # asking for something
    ["عايز", "عايزه", "عاوز", "عاوزه", "محتاج", "محتاجه", "ابغى", "نفسي", "بدي"]
    # politeness
    + ["ممكن", "لو", "سمحت", "سمحتي", "من", "فضلك", "يا", "باشا", "استاذ", "حضرتك", "ياريت"]
    # questions about the shop rather than about a garment
    + ["فين", "عندكم", "عندك", "عندكو", "فيه", "في", "مين", "ايه", "اي", "ازاي", "كام", "بكام"]
    # placeholders for the thing itself
    + ["حاجه", "حاجة", "شي", "شيء", "وريني", "اشوف", "شوف", "هات", "هاتلي", "جبلي"]
    + ["اشترى", "اشتري"]
    # particles and demonstratives
    + ["و", "او", "ال", "على", "عن", "مع", "الي", "اللي", "ده", "دي", "دا", "دى"]
    + ["هو", "هي", "انا", "احنا"]
    # the same, in English
    + ["please", "want", "need", "show", "me", "the", "a", "an", "is", "are"]
    + ["for", "with", "and", "or", "i"]
)

#: Built once: normalized key -> normalized english alternatives.
SYNONYMS: dict[str, tuple[str, ...]] = {
    normalize(key): tuple(normalize(value) for value in values)
    for key, values in _RAW_SYNONYMS.items()
}

#: Multi-word keys ("سويت شيرت", "نص سوسته") cannot be found token by token,
#: so they are substituted in the raw string first, longest first.
_PHRASE_KEYS: tuple[str, ...] = tuple(
    sorted((key for key in SYNONYMS if " " in key), key=len, reverse=True)
)


#: Egyptian word endings that carry no meaning for a search: the feminine
#: `ه`, plurals, and the possessive `ي`. Tried only as a fallback, after the
#: word itself, so `توب` is never mistaken for a shortened `توبات`.
_SUFFIXES = ("ات", "ين", "ه", "ي", "s")


def _lookup(token: str) -> tuple[str, ...]:
    """Synonyms for a token, retrying once without a meaningless ending.

    «واسعه» and «واسع» are the same word, and a customer types whichever the
    sentence needs. Listing both spellings of every entry doubles the table
    and still misses the third one.
    """
    direct = SYNONYMS.get(token)
    if direct:
        return direct
    for suffix in _SUFFIXES:
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            stemmed = SYNONYMS.get(token[: -len(suffix)])
            if stemmed:
                return stemmed
    return ()


def expand(token: str) -> set[str]:
    """One query token -> every spelling that may satisfy it.

    The token itself is always kept: a customer who types `hoodie` or `Olive`
    must still match, and a product name nobody wrote a synonym for still has
    to be findable.

    Multi-word values stay whole. Splitting `t shirts` into `t` and `shirts`
    would make `t` a token that matches every product in the shop, which is
    how "عايز تيشيرت اسود" ends up returning hoodies.
    """
    token = _strip_article(token)
    return {token, *_lookup(token)}


def query_tokens(query: str) -> list[set[str]]:
    """A query as a list of "any of these" groups, stopwords removed.

    An empty list means the query was nothing but padding -- «لو سمحت» on its
    own -- which the caller should treat as "no filter", not as "no results".
    """
    normalized = normalize(query)
    if not normalized:
        return []

    # Phrases first, so "سويت شيرت" becomes one token before splitting.
    for phrase in _PHRASE_KEYS:
        if phrase in normalized:
            normalized = normalized.replace(phrase, phrase.replace(" ", "_"))

    groups: list[set[str]] = []
    for raw in normalized.split():
        token = raw.replace("_", " ")
        if token in STOPWORDS or _strip_article(token) in STOPWORDS:
            continue
        if len(token) < 2 and not token.isdigit():
            continue
        groups.append(expand(token))
    return groups


def _contains_word(haystack: str, needle: str) -> bool:
    """A word-start match, not a bare substring.

    Prefixes are allowed on purpose -- `hoodie` has to find `hoodies` and `zip`
    has to find `zipup` -- but a match may not begin in the middle of a word.
    Plain `in` reported that `tshirt` appears in `swea·tshirt·s`, which is how
    "عايز تيشيرت" came back holding hoodies.
    """
    if not needle:
        return False
    return haystack.startswith(needle) or f" {needle}" in haystack


def matches(haystack: str, query: str) -> bool:
    """Every meaningful token in the query must match, by any of its spellings.

    AND across tokens is deliberate -- "olive hoodie" should not return every
    hoodie -- while OR within a token is what makes «زيتي» and `Olive` the
    same request.
    """
    groups = query_tokens(query)
    if not groups:
        return True
    hay = normalize(haystack)
    return all(
        any(_contains_word(hay, alternative) for alternative in group) for group in groups
    )
