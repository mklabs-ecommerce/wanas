"""The order sizes go in.

`S`, `M`, `L`, `XL` sort alphabetically to **L, M, S, XL**, and nothing in
this codebase said otherwise -- so `catalog.get_variants` handed the model its
variants in that order and the model wrote them back out in that order. From a
real conversation:

    • Lightweight Sweatpant — متوفر: Black مقاس S، و Grey مقاس M و S و XL،
      و Navy مقاس M و L و S

Nothing there is false. Every size listed is really in stock, the colours are
right, and it still reads as a shop that does not know its own stock room,
because no human being has ever recited sizes in that order. `Product.sizes`
was seeded in the right order and was the only place that knew it; every list
derived from the variants themselves lost it.

A shared kernel module rather than a catalog one because both sides need it:
`domain/services/catalog.py` reads sizes out for the bot, and
`integrations/shopify/admin_products.py` writes them in for a product staff
create -- which was sorting them alphabetically too, so the order was wrong in
the database from the moment the product existed.
"""

from __future__ import annotations

import re

#: Smallest to largest. Lower-cased on lookup, so `xl` and `XL` are one entry.
_ORDER: dict[str, int] = {
    name: index
    for index, name in enumerate(
        [
            "xxs", "xs", "s", "small", "m", "medium", "l", "large",
            "xl", "xxl", "2xl", "xxxl", "3xl",
        ]
    )
}

#: A size that is the whole run ("One Size", "Free Size") sorts first: it is
#: the only one there is, so it is never in a list with others -- and when it
#: somehow is, leading reads better than trailing.
_ONE_SIZE = {"one size", "onesize", "free size", "os"}

_NUMERIC = re.compile(r"^\d+$")

#: Past the named sizes, so a numeric run (28, 30, 32) sorts after them rather
#: than among them, and an unrecognised label after that. Both keep their own
#: internal order, which is better than scattering them through the real ones.
_AFTER_NAMED = len(_ORDER)


def sort_key(size) -> tuple:
    """Where one size belongs in a list of them.

    Unrecognised labels are never dropped or reordered against each other --
    they sort last, alphabetically, so a size this module has not heard of is
    still shown and still shown consistently.
    """
    raw = " ".join(str(size or "").split()).lower()
    if not raw:
        return (3, 0, "")
    if raw in _ONE_SIZE:
        return (0, -1, raw)
    known = _ORDER.get(raw)
    if known is not None:
        return (0, known, raw)
    if _NUMERIC.match(raw):
        return (1, _AFTER_NAMED + int(raw), raw)
    return (2, 0, raw)


def in_order(sizes) -> list:
    """The same sizes, smallest first, duplicates kept out."""
    seen = {}
    for size in sizes:
        key = " ".join(str(size or "").split()).lower()
        if key not in seen:
            seen[key] = size
    return sorted(seen.values(), key=sort_key)
