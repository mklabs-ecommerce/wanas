"""What a reply *says* it filed, checked against what it actually filed.

The sibling of `assistant/photo_claims.py`, one layer up: that module checks a
reply's words against the pictures the turn is carrying, this one checks them
against the staff-queue item the turn created. Same failure shape, same reason
it is structural rather than trusted to the prompt -- the words are the
model's and the filing is the tool layer's, and nothing joined them back
together before the reply left.

It went out in production:

    customer: «ينفع اضيفه علي نفس الاوردر اللي فات»
    bot:      «... عشان نضيف عليه قطعة جديدة، فبعتلهم الطلب»
    queue:    swap_requested -- swap Knitted Polo (Olive, XL) -> Heart Top (Black, S)

The reply is about adding. The queue item says replace, naming a garment the
customer had not mentioned and still wanted. One staff click from taking it
off his order, and the customer would have had the bot's own sentence saying
that was not what he asked for.

`request_item_add` and `request_item_swap` each return `filed` saying which
kind of request they wrote. This module reads the reply for the *other*
action's vocabulary and refuses the mismatch, which `assistant/agent.py`
handles the way it handles an unbacked photo claim: nudge and regenerate,
then a deterministic sentence rather than a wrong one.

Only a one-sided claim counts. A swap reply that says both -- «هنشيل دي
ونضيف دي» -- is describing a swap correctly, and a reply that mentions
neither has made no claim to be wrong about. The check only fires when the
reply speaks of one action and the queue holds the other.
"""

from __future__ import annotations

import re

#: Arabic short vowels and the shadda, which the model writes about half the
#: time: «هنبدّل» and «هنبدل» are the same word, and a pattern that only knows
#: one of them catches the reply only when the model happens to leave the mark
#: off. Stripped before matching rather than doubled in every alternative.
_TASHKEEL = re.compile(r"[ً-ْـٰ]")


def _normalise(text: str) -> str:
    return _TASHKEEL.sub("", text)


#: Adding. «كمان» and «زيادة» are here because the customer's own word for it
#: is usually not a verb at all -- «عايز كمان تيشيرت».
_ADD = re.compile(
    r"نضيف|أضيف|اضيف|ضيف|إضافة|اضافة|بنضيف|هنضيف|هضيف|تضيف"
    r"|نزود|نزوّد|هنزود|زيادة|كمان"
    r"|\badd(?:ing|ed)?\b|\bextra\b"
)

#: Replacing. `بدل` is deliberately not matched before «ما» or «من»: «بدل ما
#: تعمل أوردر جديد» is *instead of*, and it is what an add reply says most
#: naturally -- the production note that started this carried exactly that
#: phrase. Matching it there would have made the guard fire on the one reply
#: that was right.
_SWAP = re.compile(
    r"بدل(?!\s*(?:ما|من))|نبدل|أبدل|ابدل|هنبدل|هبدل|تبديل|استبدال|نستبدل"
    r"|نغير|نغيّر|هنغير|تغيير|نشيل|هنشيل|نرجع القطعة"
    r"|\bswap(?:ping|ped)?\b|\breplace(?:ment|s|d)?\b|\bexchange\b"
)

#: What each filed kind is allowed to sound like, and what it must not sound
#: like on its own.
_EXPECTED = {"item_add": _ADD, "item_swap": _SWAP}
_OPPOSITE = {"item_add": _SWAP, "item_swap": _ADD}


def mismatch(text: str, filed: str | None) -> str | None:
    """`None` when the reply and the queue item agree, else why they do not.

    Returns the filed kind and the one the reply described, in a string the
    log and the nudge both use: "said item_swap, filed item_add".
    """
    if not text or filed not in _EXPECTED:
        return None
    plain = _normalise(text)
    says_opposite = bool(_OPPOSITE[filed].search(plain))
    says_expected = bool(_EXPECTED[filed].search(plain))
    if says_opposite and not says_expected:
        other = "item_swap" if filed == "item_add" else "item_add"
        return f"said {other}, filed {filed}"
    return None


def filed_kind(results) -> str | None:
    """The one post-order request kind this turn filed, if exactly one.

    Two in one turn is not a shape this system produces, and guessing which
    of them the reply is about would be the same class of mistake as the one
    being closed -- so it declines to judge rather than judge wrongly.
    """
    kinds = [k for k in results if k in _EXPECTED]
    return kinds[0] if len(set(kinds)) == 1 and kinds else None
