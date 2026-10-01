"""Which language this turn's reply is written in -- decided in code.

The rule is the shop's, not the model's: a customer who writes Arabic *or*
Franco-Arabic (Arabizi -- «3ayza el top da b kam») is answered in Egyptian
Arabic script; a customer who writes English is answered in English; a mixed
message follows whichever language carries more of its words, with Franco
counted as Arabic. Left to the prompt alone the model answered Franco in
Franco half the time and English in Arabic the other half, so the decision is
made here, deterministically, and handed to the model as a per-turn note
(`turn_note`) the same way `customer_name` hands it whether to ask a name.

Catalog words are not evidence either way. «عايزة Rehla Backless Top M» is an
Arabic message with a product name in it, and «Black», «M» and «Rehla» are
what an Arabic-writing customer types too -- so sizes and the brand are
ignored, and a Latin word only counts as English when it is not Franco.

A message with no words at all (a photo, a sticker, «👍», a phone number)
says nothing about language, so the last customer message that did decides,
and a conversation that has never said anything is answered in Arabic.
"""

from __future__ import annotations

import re

ARABIC = "ar"
ENGLISH = "en"

_ARABIC_LETTER = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_WORD = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+|[A-Za-z0-9']+")

#: Arabizi spells the sounds Latin has no letter for with digits: 2 (ء), 3 (ع),
#: 5 (خ), 6 (ط), 7 (ح), 8 (ق), 9 (ص). A digit *inside* a word of letters is
#: Franco; a bare number («2», «450») is not a word at all.
_FRANCO_DIGIT = re.compile(r"(?=.*[a-z])(?=.*[2356789])^[a-z0-9']+$")

#: Common Egyptian words as they are typed in Latin letters with no digit in
#: them. Only words that are not also ordinary English ("a", "is", "me" are
#: left out on purpose), so an English sentence is never read as Franco.
_FRANCO_WORDS = frozenset(
    """
    ana enta enty inta inti ento entu enti howa heya ehna e7na ehnaa
    ayez ayza aiza ayzeen 3ayez 3ayza 3awez 3awza
    el il fe fi fel fil mn mesh msh mish mosh ma3 maa wala wla walla
    ezay ezzay izay ezayak ezayek izzayak eh eih ehh leh leeh lih feen fen fein
    emta imta kam bkam bekam bel bi keda kda kedah delwa2ty dlw2ty dlwa2ty delwa2ti
    da di dah deh dol dool kol koll kolo
    momken mumkin momkn lw lau bas bss bs aywa aiwa aywah la2 laa
    tamam tmam mashy mashi mashe tayeb tyb tb khalas yalla yala
    shokran shukran shokrn merci mersi ahlan ahln salam slam sabah masa2 masaa
    el7amdolellah alhamdulillah hamdella hamdellah enshallah inshallah insha2allah
    3ndko 3andko andko 3andak 3andek andak
    7elw 7elwa helw helwa gamed gamda awy awi 2awy khales
    m2as ma2as mqas maqas mo2as lon loon alwan
    talab otlob ab3at ab3t ba3at b3t ebaat ebaatly ab3tly
    ya yaa ahu ahi aho ady adi adeh
    """.split()  # noqa: SIM905
)

#: Not evidence of either language: the brand, sizes, and the handful of
#: catalog words an Arabic-writing customer types in Latin because that is how
#: the product is named.
_NEUTRAL = frozenset(
    """
    rehla xs s m l xl xxl xxxl one size ok okay oki
    black white pink beige brown burgundy grey gray navy olive blue green red
    top tops tee t shirt tshirt hoodie jacket cap pants
    """.split()  # noqa: SIM905
)


def _classify(word: str) -> str | None:
    if _ARABIC_LETTER.match(word):
        return ARABIC
    lowered = word.lower()
    if lowered.isdigit() or lowered in _NEUTRAL:
        return None
    if _FRANCO_DIGIT.match(lowered) or lowered in _FRANCO_WORDS:
        return ARABIC
    if len(lowered) == 1:
        return None
    return ENGLISH


def detect(text: str | None) -> str | None:
    """`ar`, `en`, or None when the text carries no word that decides.

    Counts words, not letters, so one long English product name does not
    outvote a sentence of Arabic around it. A tie goes to Arabic -- the shop's
    language, and the safer guess for an Egyptian customer.
    """
    arabic = english = 0
    for word in _WORD.findall(text or ""):
        kind = _classify(word)
        if kind == ARABIC:
            arabic += 1
        elif kind == ENGLISH:
            english += 1
    if not arabic and not english:
        return None
    return ENGLISH if english > arabic else ARABIC


def _user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def decide(text: str | None, history: list[dict] | None = None) -> str:
    """The language this turn replies in.

    The message itself decides when it can; otherwise the newest earlier
    customer message that can; otherwise Arabic.
    """
    found = detect(text)
    if found:
        return found
    for message in reversed(history or []):
        if message.get("role") != "user":
            continue
        found = detect(_user_text(message))
        if found:
            return found
    return ARABIC


_ARABIC_NOTE = """

# لغة الرد (محددة من السيستم للرسالة دي)
الزبون كاتب عربي أو فرانكو. ردك بالعامية المصرية وبالحروف العربي بس — حتى لو هو كاتب فرانكو أو فيه كلمات إنجليزي. أسماء المنتجات والمقاسات والألوان تفضل زي ما هي في الكتالوج."""

_ENGLISH_NOTE = """

# Reply language (set by the system for this message)
The customer wrote in English. Write this whole reply in clear, polite, natural English -- not Arabic, not Franco. Every rule above still holds: the same facts from the tools, the same tone (professional and warm, no pet names, never assume the customer's gender), cash on delivery only, no Markdown. Prices in EGP. Product names, sizes and colours exactly as the catalog writes them."""


def turn_note(language: str) -> str:
    """The per-turn line appended to the system prompt."""
    return _ENGLISH_NOTE if language == ENGLISH else _ARABIC_NOTE
