"""Rules a reply is held to, as code rather than as prompt lines.

Each of these began as a check in `scripts/quality_gate.py`, written after a
real conversation went wrong: a payment method the shop cannot take, the
shop's own name misspelled, a product sold as «Lightwelson», a whole line
denied without a lookup, a garment the shop does not sell answered «أيوه»,
the previous reply sent again, a sleeve length professed unknown. For as long
as they lived there they ran against a benchmark and never against a reply a
customer was about to receive.

They live here now, and both sides import them: the gate still judges
recorded runs with exactly these functions, and `assistant/agent.py` runs them
on every reply before it leaves (`correct` and `violation` at the bottom).
None of them calls a model -- a judge that can be wrong is not a rule.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from assistant import sleeve_claims
from domain.services import garments

#: The ways of paying this shop cannot take. It takes two -- cash at the door,
#: and the website's own online checkout -- and nothing in the codebase can
#: issue a refund against either, so an offer of a third is a promise made to a
#: customer who may act on it.
_OTHER_PAYMENT = (
    "فيزا", "ڤيزا", "بالفيزا", "كارت", "credit card", "بطاقة",
    "انستاباي", "إنستاباي", "instapay", "فودافون كاش", "محفظة",
    "تحويل بنكي", "paypal", "باي بال", "لينك دفع", "payment link",
)

#: "Online" is deliberately not in that list, and this is the one entry worth
#: explaining. It used to be, and it made the gate fail the shop's own correct
#: answer twice over: this *is* an online shop and says so (the prompt's first
#: line calls it «محل هدوم أونلاين»), and paying online through the website is
#: a real option here. The prompt requires that sentence verbatim --
#: «بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع» -- since 43eb403, which
#: settled it after the prompt and `assistant/comment_faq.py` had been telling
#: customers different things depending on whether they asked in a DM or in a
#: comment. A gate that fails the answer the prompt mandates is testing the
#: wrong thing, so the list above is now only the methods the shop genuinely
#: cannot take.

#: ...but talking about cash on delivery is exactly right, and some of the
#: words above appear inside perfectly correct sentences ("مش بنقبل فيزا").
#: A denial is not an offer.
#: "عند الاستلام" is the cash-on-delivery phrase itself, and deliberately not
#: the bare word "كاش" -- that one is inside "فودافون كاش", which is a payment
#: method this shop really cannot take.
_PAYMENT_DENIAL = (
    "مش", "ما بنقبل", "مابنقبلش", "غير متاح", "بس كاش", "كاش بس", "only cash",
    "عند الاستلام",
)


def offers_another_payment_method(text: str) -> str:
    """The payment method a reply offered that this shop does not have, if any.

    Deliberately a keyword check rather than a model call: a judge that can be
    wrong is not a gate, and the failure being guarded against is specific and
    literal. A sentence that *denies* the method is left alone -- "مش بنقبل
    فيزا، كاش عند الاستلام بس" is the correct answer, not a violation.
    """
    lowered = (text or "").lower()
    clauses = re.split(r"[.\n،؛!?]", text or "")
    for method in _OTHER_PAYMENT:
        if method.lower() not in lowered:
            continue
        # Look at the clause it appears in, not the whole reply: a summary can
        # correctly say "cash on delivery" in one line and nothing about cards
        # in another.
        for clause in clauses:
            if method.lower() in clause.lower() and not any(
                d.lower() in clause.lower() for d in _PAYMENT_DENIAL
            ):
                return method

    return ""


#: The shop is called Wanas Gallery and the short form is Wanas. Both are
#: correct and nothing else is.
SHOP_NAME = frozenset({"Wanas", "WANAS"})

#: The one product name that legitimately contains the brand abbreviated.
#: Masked out before the scan, so a bare `WNS` elsewhere is still caught --
#: `WNS` used as a name for the shop *is* the misspelling this rule is for.
_BOXY_WNS_TEE = re.compile(r"\bBoxy\s+WNS\s+Tee\b", re.IGNORECASE)

#: A Latin word built on the brand's consonant skeleton -- w, then n, then s,
#: with only vowels between. Catches Wnas, Wans, WNS, Wanass and the lowercase
#: slug forms, and matches almost nothing else a reply from a clothes shop
#: contains.
_BRAND_SHAPED = re.compile(r"\b[Ww][AaEeIiOoUu]*[Nn][AaEeIiOoUu]*[Ss]{1,2}[A-Za-z]*\b")

#: Ordinary English words with the same skeleton. Short list on purpose: these
#: are the only ones plausible in a reply, and a gate that guessed more widely
#: would start excusing real misspellings.
_NOT_THE_BRAND = frozenset({"wins", "wines", "wanes"})


def misspelled_shop_name(text: str) -> list[str]:
    """Every spelling of the shop's name in `text` that is not how it is spelled.

    The brand is the one word in a reply the model cannot get away with
    reconstructing: a customer who is told the shop is called something it is
    not has been given wrong information about the thing they are buying from.
    It is also the word most exposed to reconstruction, because the model sees
    it in four surface forms -- `Wanas Gallery`, `WANAS Hoodie`, `Boxy WNS Tee`
    and the Arabic «ونس» -- and Arabic writes no short vowels, so the Arabic
    form is literally w-n-s.

    Latin only, deliberately. The Arabic «وناس» is an ordinary word ("and
    people") and a rule that flagged it would fail correct replies; the
    reported failure was Latin, and this is the half that can be checked
    without guessing.
    """
    masked = _BOXY_WNS_TEE.sub(" ", text or "")
    return [
        token
        for token in _BRAND_SHAPED.findall(masked)
        if token not in SHOP_NAME and token.lower() not in _NOT_THE_BRAND
    ]


#: Ordinary English a reply may contain that happens to sit one or two edits
#: from a catalog word. Kept deliberately short: every entry here is a real
#: word this shop's replies use, and a longer list would start excusing the
#: garbles this rule exists to catch.
_ORDINARY_ENGLISH = frozenset(
    {
        "size", "sizes", "small", "medium", "large", "color", "colour", "colors",
        "colours", "price", "order", "shirt", "shirts", "short", "sleeve",
        "sleeves", "long", "half", "cash", "online", "free", "new", "sale",
    }
)

#: Below this length an edit-distance-1 neighbour is usually a different word
#: rather than a typo of the same one ("navy"/"nay"), so the scan starts at
#: names long enough for the comparison to mean something.
_NAME_MIN = 6


def _edits(word: str, target: str) -> int:
    """Levenshtein distance."""
    previous = list(range(len(target) + 1))
    for i, a in enumerate(word, 1):
        current = [i]
        for j, b in enumerate(target, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b))
            )
        previous = current
    return previous[-1]


#: How much of a catalog word a garble keeps before it goes wrong. Seven
#: characters of `Lightweight` survive into `Lightwelson`, which is the shape
#: of reconstruction: the model remembers how the name starts and invents the
#: rest. Distance alone does not catch it -- those two are four edits apart,
#: and a threshold loose enough to include them would flag half of English.
_PREFIX_MATCH = 6


def _is_near_miss(word: str, target: str) -> bool:
    """`word` looks like a mangled `target` rather than a different word.

    A plural, a singular or any other extension is not a mangling: `Sweatpant`
    and `Sweatpants` are the same name, and flagging one would fail correct
    replies. Only a word that *diverges* from a catalog word counts.
    """
    if word == target or word.startswith(target) or target.startswith(word):
        return False
    if abs(len(word) - len(target)) <= 2 and _edits(word, target) <= 2:
        return True
    # Same opening, different word. Requires both to be long enough that a
    # shared six-character prefix is a fact rather than a coincidence.
    if len(word) >= 8 and len(target) >= 8:
        shared = 0
        for a, b in zip(word, target, strict=False):
            if a != b:
                break
            shared += 1
        return shared >= _PREFIX_MATCH
    return False


def garbled_catalog_words(text: str, vocabulary) -> list[str]:
    """Latin words in `text` that are *nearly* a catalog word but are not one.

    The failure this is for, from a real conversation: the shop sells a
    `Lightweight Sweatpant` and the bot called it a **Lightwelson** Sweatpant
    -- six times, across an hour, in every message that mentioned it. Every
    other check passed. The price was right, the colours were right, the
    Arabic was good, and the customer was being sold a product this shop does
    not have, by a name nobody could search for.

    A product name is the one part of a reply that is *quoted*, not composed:
    it arrives in a tool result and has to come out byte for byte. So the test
    is not "is this word English" but "is this word a near-miss of something
    in our catalog" -- which is what reconstruction from memory looks like,
    and almost never what ordinary prose looks like.
    """
    known = {w.lower() for w in vocabulary if len(w) >= _NAME_MIN}
    if not known:
        return []
    found = []
    for word in re.findall(r"[A-Za-z][A-Za-z-]{4,}", text or ""):
        lowered = word.lower()
        if lowered in known or lowered in _ORDINARY_ENGLISH:
            continue
        near = [k for k in known if _is_near_miss(lowered, k)]
        if near:
            found.append(f"{word!r} (did you mean {sorted(near)[0]!r}?)")
    return found


def catalog_vocabulary(session) -> list[str]:
    """Every Latin word the catalog actually contains, for the rule above."""
    from domain.models import Product, Variant

    words: set[str] = set()
    for (name,) in session.execute(select(Product.name)).all():
        words.update(re.findall(r"[A-Za-z][A-Za-z-]*", name or ""))
    for (color,) in session.execute(select(Variant.color).distinct()).all():
        words.update(re.findall(r"[A-Za-z][A-Za-z-]*", color or ""))
    for (category,) in session.execute(select(Product.category).distinct()).all():
        words.update(re.findall(r"[A-Za-z][A-Za-z-]*", category or ""))
    for (raw,) in session.execute(select(Product.style)).all():
        for style in raw or []:
            words.update(re.findall(r"[A-Za-z][A-Za-z-]*", style))
    return sorted(words)


#: The sleeve words, as a reply would write them. Deliberately the shop's own
#: Arabic plus the English the model falls back to -- this is matched against
#: what *went out*, not against what the customer typed.
_SLEEVE_WORDS = (
    "نص كم", "نُص كم", "نصف كم", "كم قصير", "كم طويل", "من غير كم", "بدون كم",
    "طول الكم", "الكم", "half sleeve", "short sleeve", "long sleeve", "sleeveless",
)

#: "I don't know" in every shape a reply has reached for. Used as-is only by
#: the offline quality gate, which has no conversation to check against. A
#: live turn asks `assistant/sleeve_claims.py` instead, which fails these
#: only when the product in question *has* a sleeve recorded: a product
#: nobody classified has none (`domain/services/sleeves.py`), and for it
#: «مش متسجّل» is the true answer -- the rule that forbade it everywhere is
#: what left an inferred «كم طويل» as the only sentence the bot could say.
_NO_DATA = (
    "معنديش المعلومة", "معنديش معلومات", "معنديش بيانات", "مش متوفرة عندي",
    "مفيش معلومات", "مفيش بيانات", "مش موجودة عندي", "مش عارف", "مش عارفة",
    "مش متسجّل", "مش متسجل", "غير مسجل", "مش مسجل", "مش مسجّل",
    "لسه متسجلش", "لسه متسجّلش", "محدش سجل", "محدش سجّل",
    "أتأكد من الفريق", "اتأكد من الفريق", "أحولك", "احولك", "أحوّلك",
    "no data", "no information", "not available", "don't have", "not recorded",
)


def dodged_a_sleeve_question(text: str) -> str:
    """The sentence a reply used to get out of answering about sleeve length.

    A customer asked for «البولو النص كم» and was told the shop has two polos
    and no published data about sleeve length for either, plus an offer to
    fetch a person -- about a polo that is on the shelf and is half-sleeve.

    The first fix gave the catalog a `sleeve` field and left it unset for
    anything outside the half-sleeve list, which moved the same sentence
    rather than removing it: every hoodie, jacket and sweatpant then answered
    "nobody has recorded that". That is worse than the original bug, because
    it is the shop saying it does not know what it sells, about most of what
    it sells.

    Offline only now (the quality gate): a live turn checks the claim
    against the record for the product in question instead
    (`assistant/sleeve_claims.py`), because "not recorded" is the truth about
    a product nobody classified.
    """
    lowered = (text or "").lower()
    if not any(word.lower() in lowered for word in _SLEEVE_WORDS):
        return ""
    return next((phrase for phrase in _NO_DATA if phrase.lower() in lowered), "")


#: Claims that the shop does not stock a whole *line* of things -- a section,
#: a department, a category. Deliberately not the narrow denials ("we're out
#: of olive", "no half-sleeve hoodie"): those are answers to a lookup, and are
#: often correct. These are statements about what the business sells, and the
#: model has no way to know one without asking.
_SECTION_DENIALS = (
    "مفيش قسم", "مافيش قسم", "مفيش عندنا قسم", "مش عندنا قسم",
    "مش بنبيع", "مابنبيعش", "مبنبيعش", "مفيش نوع",
    "we don't sell", "we do not sell", "no section", "we don't have a section",
)


def denies_a_whole_section(text: str) -> str:
    """The phrase in which a reply told a customer this shop has no such line.

    From the audited conversation: a customer asked for «حريمي» -- womenswear
    -- and was told «مفيش قسم حريمي لوحده», with no tool called in the turn.
    The shop has a women's department with two products in it, and
    `search_terms` already maps «حريمي» onto `women`, so the lookup that would
    have answered it correctly was one call away and simply never happened.

    That is the most expensive shape of answering from memory: a price quoted
    from memory is checked at the door, but a customer told the shop does not
    sell what they came for leaves, and nothing about the conversation looks
    like a failure afterwards.
    """
    lowered = (text or "").lower()
    return next((phrase for phrase in _SECTION_DENIALS if phrase.lower() in lowered), "")


#: Above this, two replies are the same reply. Not 1.0: the model re-words a
#: line or drops a bullet while saying the identical thing, and "nearly all of
#: it again" is the failure -- an exact-match rule would miss every real
#: instance of it.
_REPEAT_RATIO = 0.85


def repeats_the_previous_reply(text: str, previous: str) -> float:
    """How much of the previous reply this one says again, 0.0 to 1.0.

    From the audited conversation. The bot listed two sweatpants and asked
    "photos, or sizes?"; the customer answered «الاتنين» -- both -- and the
    bot replied with the *same two lines again* and asked which of the two
    products they meant. The customer had answered a question about photos and
    sizes and was handed back the list they were already looking at.

    A reply that repeats the one before it has, by definition, not used the
    message in between. Whatever the customer said, the answer cannot be the
    previous answer -- if they asked for something the bot cannot do, it says
    so; if they were unclear, it asks something *new*. Saying it all again is
    the one response that carries no information at all.

    Compared on the text as written, whitespace folded, because that is what
    the customer reads.
    """
    import difflib

    now = " ".join((text or "").split())
    before = " ".join((previous or "").split())
    if not now or not before:
        return 0.0
    return difflib.SequenceMatcher(None, before, now).ratio()


#: Saying yes. Any of these beside a garment the shop does not sell is the
#: failure this rule is for -- the customer asked for X, X is not on the shelf,
#: and the reply opened by confirming it before listing something else.
_AFFIRMATIONS = (
    "أيوه", "ايوه", "أيوة", "ايوة", "اه عندنا", "آه عندنا", "طبعا", "طبعاً",
    "عندنا كتير", "اكيد", "أكيد", "yes", "sure", "of course",
)

#: Saying no. One of these has to be in the reply -- the customer is owed the
#: plain sentence before anything is offered as a substitute.
_DENIALS = (
    "مفيش", "ما فيش", "مافيش", "معندناش", "معنداش", "مش بنبيع", "مابنبيعش",
    "مبنبيعش", "مش عندنا", "مش متوفر", "للأسف", "للأسف", "مش من اللي بنبيعه",
    "we don't have", "we do not have", "we don't sell", "we do not sell",
)


def offered_a_garment_they_did_not_ask_for(customer: str, reply: str) -> str:
    """The reply answered a garment we do not sell as though we did.

    From a real conversation:

        customer: «فيه قمصان»
        bot:      «أيوه، عندنا تيشيرتات كتير:» + four t-shirts

    In Egyptian a قميص is a button-up shirt. A تيشيرت is not one, this shop
    sells no shirts, and «أيوه عندنا» is the shop saying yes to something it
    does not have and then handing over four of something else underneath that
    yes. It is the `مفيش قسم حريمي` failure with the sign flipped: that one
    denied what the shop has, this one affirms what it does not.

    The rule is deliberately about the *plain sentence*, not about the
    alternatives. Offering the t-shirts is right and is what the tool's
    `alternatives` exist for; offering them without first saying we have no
    shirts is what turns an honest answer into a wrong one. So a reply fails
    when it names no denial at all, and fails harder when it opens with a yes.
    """
    found = garments.not_sold(customer or "")
    if found is None:
        return ""
    label = found[0]
    lowered = (reply or "").lower()
    affirmed = next((word for word in _AFFIRMATIONS if word.lower() in lowered), "")
    if affirmed and not any(word.lower() in lowered for word in _DENIALS):
        return f"opened with {affirmed!r} about {label}"
    if not any(word.lower() in lowered for word in _DENIALS):
        return f"never said we have no {label}"
    return ""




# --------------------------------------------------------------------------
# live: what the turn does with each rule
# --------------------------------------------------------------------------
#
# Two kinds, decided by one question: is there exactly one right answer?
#
# * `correct` -- yes. A catalog word misspelled has one right spelling, an
#   internal order id has one reference, the shop has one name. The reply is
#   fixed in place and nothing is regenerated for it.
# * `violation` -- no. A payment method the shop cannot take, a line denied
#   without looking, a «أيوه» to a garment not sold, a sleeve question dodged,
#   the last reply sent again: the fix is a different sentence, and only the
#   model can write it. The turn is sent back with the reason.


def _catalog_word(word: str, vocabulary) -> str | None:
    """The catalog word `word` is a mangling of, when there is exactly one."""
    lowered = word.lower()
    # One spelling per word, and the name's own when the vocabulary has two:
    # `Lightweight` the product name over `lightweight` the style tag.
    known: dict[str, str] = {}
    for w in vocabulary:
        if len(w) >= _NAME_MIN and (w.lower() not in known or w[:1].isupper()):
            known[w.lower()] = w
    if lowered in known or lowered in _ORDINARY_ENGLISH:
        return None
    near = sorted({known[k] for k in known if _is_near_miss(lowered, k)})
    return near[0] if len(near) == 1 else None


#: The brand misspelt as a word the model built from its vowels. `WNS` is
#: deliberately not rewritten: it is also the start of an internal order id
#: and part of the Boxy WNS Tee's name, and a "fix" there would be the damage.
_BRAND_FIX = "Wanas"

_ORDER_ID = re.compile(r"\bWNS-\d+\b")

#: Emoji, and the joiners and selectors that make one out of several.
_EMOJI_CHAR = "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u2300-\u23ff]"
_EMOJI_MODS = "[\ufe0f\U0001f3fb-\U0001f3ff]*"
_EMOJI = re.compile(f"{_EMOJI_CHAR}{_EMOJI_MODS}(?:\u200d{_EMOJI_CHAR}{_EMOJI_MODS})*")
_APOLOGY = re.compile(r"معلش|آسف|اسف|أعتذر|اعتذر|للأسف|للاسف")


def limit_emoji(text: str, *, states_money: bool) -> str:
    """At most one emoji, and none beside a price or an apology.

    The prompt's rule, as code: "one simple emoji at most, never in a price,
    an order's status, a complaint or an apology". A price and an apology are
    the two of those a reply's own words give away.
    """
    found = list(_EMOJI.finditer(text or ""))
    if not found:
        return text
    keep_first = not states_money and not _APOLOGY.search(text)
    out, last = [], 0
    for index, match in enumerate(found):
        out.append(text[last : match.start()])
        if index == 0 and keep_first:
            out.append(match.group(0))
        last = match.end()
    out.append(text[last:])
    cleaned = "".join(out)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return re.sub(r"[ \t]+\n", "\n", cleaned).strip()


#: Persian and Urdu letters that look like Arabic ones and are not: the
#: prompt itself carried «متنادیش» with a Persian ی for as long as it existed,
#: and a model copies the letters it is shown. On a phone they render almost
#: the same and search, copy-paste and screen readers treat them as different
#: words.
_LOOKALIKE_LETTERS = str.maketrans({"\u06cc": "\u064a", "\u06a9": "\u0643", "\u06be": "\u0647"})

#: Misspellings with exactly one right answer, each seen in a real reply.
#: Whole words only (`_WORD_EDGE`), so a longer word containing one is never
#: touched. «كل تمام» is the one that started this list -- the answer to
#: «عامل ايه» that a person reads as broken Arabic, not as a typo.
_MISSPELLINGS = (
    ("كل تمام", "كله تمام"),
    ("العفى", "العفو"),
    ("تقلي", "تقولي"),
    ("أبراهم", "أبرزهم"),
    ("إنشاء الله", "إن شاء الله"),
    ("انشاء الله", "إن شاء الله"),
    ("لاكن", "لكن"),
    ("أحد من الفريق", "حد من الفريق"),
)
#: \w already covers Arabic letters; the harakat are added so a vowelled
#: word is still one word. Not the whole Arabic block: «،» and «؟» live there.
_WORD_EDGE = r"(?<![\w\u064b-\u065f])({})(?![\w\u064b-\u065f])"

#: The internal name of the order number, which the prompt used to *teach*
#: («قول الـ reference»), and so reached customers as «ابعتلي رقم الأوردر
#: (الـ reference)». To the customer it is «رقم الأوردر» and nothing else.
_REFERENCE_ASIDE = re.compile(r"\s*\((?:ال(?:ـ)?\s*)?reference\)", re.IGNORECASE)
_REFERENCE_WORD = re.compile(r"(?:ال(?:ـ)?\s*)reference\b", re.IGNORECASE)


def fix_arabic(text: str) -> tuple[str, list[str]]:
    """The Arabic slips that have one right spelling, fixed, and what changed."""
    fixes: list[str] = []
    fixed = (text or "").translate(_LOOKALIKE_LETTERS)
    if fixed != (text or ""):
        fixes.append("persian letters")
    for wrong, right in _MISSPELLINGS:
        # «و» is written joined to the word after it: «وكل تمام» is the same slip.
        pattern = _WORD_EDGE.format("و?" + re.escape(wrong))
        fixed, count = re.subn(
            pattern, lambda m, r=right, w=wrong: m.group(1)[: -len(w)] + r, fixed
        )
        if count:
            fixes.append(f"{wrong} -> {right}")
    for pattern, replacement in ((_REFERENCE_ASIDE, ""), (_REFERENCE_WORD, "رقم الأوردر")):
        fixed, count = pattern.subn(replacement, fixed)
        if count:
            fixes.append("reference -> رقم الأوردر")
    return fixed, fixes


#: Words that are not Egyptian. Each is ordinary in another Arabic -- Levantine,
#: Gulf, or the Modern Standard of a form letter -- and each reads to an
#: Egyptian customer as a shop that is not talking to them: «بتكون وين» went
#: out where «فين» was the only word an Egyptian would use. None has a single
#: mechanical replacement (the sentence around «لدينا» is MSA too), so the
#: reply is written again rather than patched.
_NOT_EGYPTIAN = (
    ("وين", "فين"),
    ("شو", "إيه"),
    ("هيك", "كده"),
    ("منيح", "كويس"),
    ("هلق", "دلوقتي"),
    ("بدك", "عايز"),
    ("تبعك", "بتاعك"),
    ("تبعنا", "بتاعنا"),
    ("شلون", "إزاي"),
    ("لدينا", "عندنا"),
    ("لديك", "عندك"),
    ("لديكم", "عندكم"),
    ("سوف", "هـ"),
    ("يرجى", "ياريت"),
    ("هل ترغب", "تحب"),
    ("هل تريد", "تحب"),
)


def not_egyptian(text: str) -> str:
    """The first non-Egyptian word in a reply, with the one to use, or ""."""
    for word, egyptian in _NOT_EGYPTIAN:
        if re.search(_WORD_EDGE.format(re.escape(word)), text or ""):
            return f"«{word}» -> «{egyptian}»"
    return ""


def correct(
    text: str, *, vocabulary, references: dict[str, str], states_money: bool
) -> tuple[str, list[str]]:
    """`text` with every rule that has one right answer applied, and what changed.

    `vocabulary` is the catalog's own words (`catalog_vocabulary`), and
    `references` maps this customer's internal order ids to the reference
    they were given (`#1040`) -- the one they can quote to staff.
    """
    text, fixes = fix_arabic(text)

    def _order(match: re.Match) -> str:
        reference = references.get(match.group(0))
        if reference and reference != match.group(0):
            fixes.append(f"{match.group(0)} -> {reference}")
            return reference
        return match.group(0)

    text = _ORDER_ID.sub(_order, text or "")

    def _word(match: re.Match) -> str:
        word = match.group(0)
        if word.count("-") >= 2:
            return word  # an identifier (`wanas-hoodie-s-black`), not a name
        right = _catalog_word(word, vocabulary)
        if right is None:
            return word
        fixes.append(f"{word} -> {right}")
        return right

    text = re.sub(r"[A-Za-z][A-Za-z-]{4,}", _word, text)

    for wrong in misspelled_shop_name(text):
        if wrong.upper() == "WNS" or wrong.lower().startswith("wns"):
            continue
        # A standalone word only -- never inside an identifier or a link
        # (`wanas-hoodie`, `wanas.eg`), where "fixing" it breaks the thing.
        standalone = rf"(?<![\w\-/.]){re.escape(wrong)}(?![\w\-/.])"
        text, count = re.subn(standalone, _BRAND_FIX, text)
        if count:
            fixes.append(f"{wrong} -> {_BRAND_FIX}")

    limited = limit_emoji(text, states_money=states_money)
    if limited != text:
        fixes.append("emoji")
    return limited, fixes


#: Below this, two replies are too short to be "the same reply" in a way
#: that matters: «تحب أساعدك في إيه؟» twice is a greeting, not a failure.
_REPEAT_MIN_CHARS = 40

#: The catalog lookups. A reply saying the shop has no such line after one of
#: these has an answer behind it; before any of them it is memory.
_CATALOG_TOOLS = {"get_products", "get_categories", "get_variants"}


def violation(
    text: str,
    *,
    customer: str,
    previous: str,
    results: list[tuple[str, dict]],
    history: list[dict] | None = None,
) -> str:
    """The rule this reply breaks that only a new sentence can fix, or "".

    `results` are this turn's `(tool name, result)` pairs. `history` is the
    conversation so far; with it, sleeve claims are held to what the tools
    recorded for the products in question (`assistant/sleeve_claims.py`).
    Without it -- the offline quality gate -- the older, blunter sleeve rule
    applies.
    """
    called = {name for name, _content in results}

    method = offers_another_payment_method(text)
    if method:
        return f"payment: offered {method!r}, which this shop does not take"

    if not called & _CATALOG_TOOLS:
        denial = denies_a_whole_section(text)
        if denial:
            return f"section: said {denial!r} without looking it up"

    refused = any(
        name == "get_products"
        and isinstance(content, dict)
        and content.get("error") == "garment_not_sold"
        for name, content in results
    )
    lowered = (text or "").lower()
    if refused and not any(word.lower() in lowered for word in _DENIALS):
        return "garment: the tool said we do not sell it and the reply never says so"
    if not called & _CATALOG_TOOLS and garments.not_sold(customer or ""):
        found = offered_a_garment_they_did_not_ask_for(customer, text)
        if found.startswith("opened with"):
            return f"garment: {found}"

    dialect = not_egyptian(text)
    if dialect:
        return f"dialect: {dialect}"

    if history is not None:
        wrong = sleeve_claims.problem(text, history)
        if wrong:
            return f"sleeve: {wrong}"
    else:
        dodge = dodged_a_sleeve_question(text)
        if dodge:
            return f"sleeve: said {dodge!r} beside a sleeve word"

    if (
        len(" ".join((text or "").split())) >= _REPEAT_MIN_CHARS
        and repeats_the_previous_reply(text, previous) >= _REPEAT_RATIO
    ):
        return "repeat: the previous reply, sent again"
    return ""
