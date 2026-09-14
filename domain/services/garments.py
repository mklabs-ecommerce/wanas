"""Which garment an Egyptian customer is actually naming, and whether we sell it.

    customer: «فيه قمصان»
    bot:      «أيوه، عندنا تيشيرتات كتير:» + four t-shirts

In Egyptian Arabic a **قميص is a button-up shirt** -- a collar, a placket,
buttons all the way down -- and it is not a تيشيرت. This shop does not sell
one. The reply above is not a near-miss: it is the shop saying yes to something
it does not have, and then handing the customer four of something else under
that yes. The customer either believes it and is disappointed at the door, or
reads it as the shop not listening.

The word was in `search_terms.py` as `"قميص": ("tee", "polo", "shirts")`, which
is exactly the kind of entry that table is *for* -- translating a customer's
Arabic into the catalog's English -- applied to a word that has no translation
here, because the garment is not on the shelf. Modern Standard Arabic is where
that mapping comes from: in MSA قميص is a generic upper-body garment, and a
t-shirt can be called قميص قصير الأكمام. In Egyptian it cannot. The shop writes
and reads Egyptian.

There are two halves to the garment vocabulary and they answer two different
questions. The other half is not here:

* **Names for things we do sell** stay in `search_terms.py`, which is where a
  customer's Arabic has always been translated into the catalog's English, and
  which got the missing Egyptian names in the same change: «فانلة» is a
  t-shirt, «بلوفر» and «سويتر» and «كنزة» are the sweatshirt, «بنطال» and
  «بنطرون» are the sweatpant. Missing one of those is the mirror-image failure
  -- telling a customer we have nothing when it is on the shelf.
* **Names for things we do not sell** are `NOT_SOLD` below, each with the
  nearest thing we do. A garment we do not sell gets a plain "we don't have
  that", and the alternatives are offered *as alternatives*, never as the
  thing itself. That difference is the whole of it.

The shop's six categories are T-Shirts, Polo Shirts, Hoodies & Sweatshirts,
Joggers & Sweatpants, Jackets and Tops. Everything else a customer can name --
shoes, a suit, shorts, a tracksuit set, jeans, a bag -- belongs here. Adding a
product category means moving its words from one half to the other, and
`tests/test_garment_vocabulary.py` fails if a word is in both.
"""

from __future__ import annotations

from domain.services.search_terms import normalize

#: Egyptian names for garments this shop does **not** sell: `{word: (what it
#: is called back to the customer, the categories worth offering instead)}`.
#:
#: The second half is deliberately a list of the shop's own categories rather
#: than free text: an alternative is only honest if it is something on the
#: shelf, and the tool goes and reads it rather than the model describing it.
#: An empty tuple means there is nothing close enough to offer -- for shoes
#: and a suit there genuinely is not, and inventing a bridge to a hoodie is
#: how "we don't sell that" turns back into "here are four t-shirts".
NOT_SOLD: dict[str, tuple[str, tuple[str, ...]]] = {
    # --- the one that started this ---------------------------------------
    # A button-up shirt. Not a تيشيرت, not a بولو -- a different garment with
    # a different collar, a different placket and a different occasion. MSA
    # uses قميص generically; Egyptian does not, and the shop writes Egyptian.
    "قميص": ("قميص", ("T-Shirts", "Polo Shirts")),
    "قمصان": ("قميص", ("T-Shirts", "Polo Shirts")),
    "قميس": ("قميص", ("T-Shirts", "Polo Shirts")),
    "كاچوال شيرت": ("قميص", ("T-Shirts", "Polo Shirts")),
    "button up": ("قميص", ("T-Shirts", "Polo Shirts")),
    "button-up": ("قميص", ("T-Shirts", "Polo Shirts")),
    "dress shirt": ("قميص", ("T-Shirts", "Polo Shirts")),
    "formal shirt": ("قميص", ("T-Shirts", "Polo Shirts")),
    # --- a matching set, which is not the same as the two halves ----------
    # The shop sells sweatpants and it sells sweatshirts; it does not sell
    # them as a set, and a customer asking for a تراكسوت is asking for the
    # set. Saying yes and sending a sweatpant is the قميص mistake again -- so
    # the two halves are offered, named as two separate pieces.
    "تراكسوت": ("تراكسوت كامل", ("Joggers & Sweatpants", "Hoodies & Sweatshirts")),
    "تراك سوت": ("تراكسوت كامل", ("Joggers & Sweatpants", "Hoodies & Sweatshirts")),
    "طقم رياضي": ("تراكسوت كامل", ("Joggers & Sweatpants", "Hoodies & Sweatshirts")),
    "tracksuit": ("تراكسوت كامل", ("Joggers & Sweatpants", "Hoodies & Sweatshirts")),
    # --- trousers we do not make ------------------------------------------
    "شورت": ("شورت", ("Joggers & Sweatpants",)),
    "شورتات": ("شورت", ("Joggers & Sweatpants",)),
    "شور": ("شورت", ("Joggers & Sweatpants",)),
    "برمودا": ("شورت", ("Joggers & Sweatpants",)),
    "shorts": ("شورت", ("Joggers & Sweatpants",)),
    "جينز": ("بنطلون جينز", ("Joggers & Sweatpants",)),
    "جنز": ("بنطلون جينز", ("Joggers & Sweatpants",)),
    "jeans": ("بنطلون جينز", ("Joggers & Sweatpants",)),
    # --- not clothes at all, or nothing like them on the shelf ------------
    # No alternatives on purpose. There is no nearest thing to a pair of
    # shoes in a shop that sells none, and offering a hoodie to someone
    # asking for trainers is the same sentence this module exists to stop.
    "جزمة": ("جزم", ()),
    "جزم": ("جزم", ()),
    "شوز": ("جزم", ()),
    "سنيكرز": ("جزم", ()),
    "كوتشي": ("جزم", ()),
    "بوت": ("جزم", ()),
    "shoes": ("جزم", ()),
    "sneakers": ("جزم", ()),
    "بدلة": ("بدلة", ()),
    "بدل": ("بدلة", ()),
    "بدله": ("بدلة", ()),
    "suit": ("بدلة", ()),
    "شنطة": ("شنط", ()),
    "شنط": ("شنط", ()),
    "باك باك": ("شنط", ()),
    "كاب": ("كابات", ()),
    "كابات": ("كابات", ()),
    "طاقية": ("كابات", ()),
    "شراب": ("شرابات", ()),
    "شرابات": ("شرابات", ()),
    "فستان": ("فساتين", ("Tops",)),
    "فساتين": ("فساتين", ("Tops",)),
    "عباية": ("عبايات", ()),
    "بيجامة": ("بيجامات", ()),
    "بيچامة": ("بيجامات", ()),
}


#: Normalized once, the same way a query is, so the comparison is between two
#: folded strings and not between a diacritic and a hamza.
_NOT_SOLD_NORMALIZED: dict[str, tuple[str, tuple[str, ...]]] = {
    normalize(word): value for word, value in NOT_SOLD.items()
}

#: The longest words first, so «تراك سوت» is recognised before «تراك» (which
#: *is* something we sell) can claim it.
_NOT_SOLD_ORDER: tuple[str, ...] = tuple(
    sorted(_NOT_SOLD_NORMALIZED, key=lambda word: (-len(word), word))
)

#: The bare English word, which needs a rule of its own. The model translates
#: the customer's Arabic before it calls the tool, so «فيه قمصان» arrives here
#: as `shirts` about half the time -- and `shirts` is also the second half of
#: `T-Shirts` and `Polo Shirts`, which are two of the six things this shop
#: does sell. Matched as a standalone word whose *preceding* word is not one
#: of the garment kinds below.
_ENGLISH_SHIRT = ("shirt", "shirts")
_SHIRT_PREFIXES = frozenset({"t", "tee", "tees", "polo", "polos", "sweat", "sweats"})
_SHIRT_ANSWER = ("قميص", ("T-Shirts", "Polo Shirts"))


def _words(text: str) -> list[str]:
    return normalize(text).split()


def not_sold(text: str) -> tuple[str, tuple[str, ...]] | None:
    """The garment in `text` that this shop does not sell, or None.

    Returns `(what to call it back, the categories worth offering instead)`.

    Whole words only, and against the *normalized* string, so «قمصان؟» and
    «قُمصان» both land. A multi-word entry («تراك سوت», «button up») is matched
    against the joined string, longest first -- which is also what keeps
    «تراك سوت» (a set we do not sell) from being claimed by «تراك» (a
    sweatpant, which we do).

    A query naming both -- «عايز تيشيرت مش قميص» -- still answers about the
    قميص, and that is correct: the customer is owed the plain "we don't have
    those", and the t-shirts they asked for are exactly what the alternatives
    then offer.
    """
    words = _words(text)
    if not words:
        return None
    joined = " ".join(words)
    for word in _NOT_SOLD_ORDER:
        if word in words or (" " in word and word in joined):
            return _NOT_SOLD_NORMALIZED[word]
    for index, word in enumerate(words):
        if word in _ENGLISH_SHIRT and (index == 0 or words[index - 1] not in _SHIRT_PREFIXES):
            return _SHIRT_ANSWER
    return None
