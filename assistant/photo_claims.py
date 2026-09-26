"""What a reply *says* about photographs, checked against what it is sending.

A turn produces exactly one message. The words are composed by the model and
the pictures are attached by the tool layer, and nothing joins the two back
together before the reply leaves -- so a sentence saying «دي صورتهم الاتنين 👆»
can go out beside one photograph, or beside none at all, and read to the
customer as a delivery that happened. It did happen, in production:

    bot:      «دي صورتهم الاتنين 👆»              -> one photo went, one failed
    customer: «انت باعت صوره واحد مش اتنين»
    bot:      «دي صورة Lightweight الأسود 👆»     -> no photo at all
    customer: «انت مش باعت صور اصلا»
    bot:      «... ممكن تكون مشكلة في النت — جرب اقفل الواتس وافتحه تاني»

Three separate things are decided here, and each is structural rather than a
phrase list wherever it can be:

* **`unbacked_claim`** -- the sentence claims a picture the reply is not
  carrying. Not "does it mention photos" (that was the old rule and it only
  caught the empty case): the claim is read *per clause*, so a clause naming
  two products beside the word «صورة» is a claim on two photographs, and a
  clause saying «الاتنين» is a claim on two whichever products it names. The
  count of attachments is the fact it is checked against.
* **`blames_the_customer`** -- the reply explains our own failed send by the
  customer's phone, app or connection. There is no version of that which is
  right: when the customer says a photo did not arrive, that is ground truth.
* **`undelivered`** -- the photographs an earlier reply in *this* conversation
  tried to send and the platform refused. Written onto the stored message by
  `assistant/session.py::record_undelivered_attachments` after the send, which
  is the only moment it is knowable, and read back here so the next turn
  argues with nobody.

Nothing in this module calls a model, and nothing in it guesses: a product is
"claimed" only when its name came out of a tool result in this same
conversation.
"""

from __future__ import annotations

import re

from assistant.messages import ASSISTANT, TOOL_RESULTS

#: The word "photo", in the forms the model actually writes it. Deliberately
#: not paired with a sending verb: Arabic has too many ways to say it
#: («هبعتلك», «اتفضل», «دي», «جاية»), and every previous attempt to enumerate a
#: phrase list is what let the next phrasing through.
_IMAGE_WORD = re.compile(r"صور|صوره|صورة|\bphotos?\b|\bpictures?\b|\bimages?\b")

#: The one sanctioned reason to mention photos while sending none: the product
#: has none. The prompt asks for exactly this sentence, so it must not be
#: retried -- nudging the model off it is nudging it towards inventing a
#: picture.
_NO_IMAGES = re.compile(
    r"(?:مفيش|ما فيش|معندناش|معنديش|ملقيتش|مش متاح|مش موجود|no|don'?t have|do not have)"
    r"[^\n]{0,20}?(?:صور|صوره|صورة|photo|picture|image)"
)

#: "Both of them" / "all of them", answering a choice the bot itself offered.
#: «الاتنين» beside a photo word is a claim on two photographs and nothing
#: else, whether or not the sentence names the products.
_BOTH = re.compile(r"الاتنين|الاثنين|الإتنين|كلاهما|\bboth\b")
_ALL = re.compile(r"كلهم|كلّهم|جميعهم|\ball of them\b")

#: A plural photograph. «صور» and «الصور» stand alone; «صورهم»/«صورتهم» are the
#: possessive forms the audited reply actually used.
_PLURAL_PHOTO = re.compile(
    r"(?<![^\W\d_])(?:ال)?صور(?![^\W\d_])|صورهم|صورتهم|صورتين"
    r"|\bphotos\b|\bpictures\b|\bimages\b"
)

#: Where one claim ends and the next begins. A reply is read clause by clause
#: so that "here is X's photo, and we also have Y" is not read as a claim on a
#: photograph of Y.
_CLAUSE = re.compile(r"[.\n،؛!؟?]+")

#: A clause about the size chart rather than the garment. «دي صورة جدول
#: المقاسات 👆» beside the chart it attached is a true sentence, and it was
#: read as a photo promised with nothing sent -- because a chart is rightly
#: not a photograph *of the garment* -- and retried until the model stopped
#: mentioning the picture it had sent. Deliberately not «مقاسات» on its own:
#: «صورة التيشيرت والمقاسات المتاحة» is about the shirt.
_CHART_WORD = re.compile(r"جدول|قياسات|size ?chart|\bchart\b")


def _is_chart(labels: dict[str, dict], path: str) -> bool:
    return str(((labels or {}).get(path) or {}).get("label") or "").endswith("size chart")


def _garment_claim(text: str) -> bool:
    """Some clause claims a photograph and is not about the size chart."""
    for clause in _CLAUSE.split(text or ""):
        lowered = clause.lower()
        if not _IMAGE_WORD.search(lowered) or _NO_IMAGES.search(lowered):
            continue
        if not _CHART_WORD.search(lowered):
            return True
    return False


def mentions_photo(text: str) -> bool:
    """The reply talks about photographs at all."""
    return bool(text) and bool(_IMAGE_WORD.search(text.lower()))


def promises_images(text: str) -> bool:
    """A reply that talks about photos in a turn that attached none.

    The caller checks the attachments; this only decides whether the sentence
    claims a picture is coming. Same invariant as a dangling promise and the
    same reason behind it: no second message is ever produced for a turn, so
    "حاضر، هبعتلك صور كل الألوان" with an empty `attachments` list is not a
    slow reply, it is the last thing the customer hears.

    Two exemptions, and both are cases where the reply is doing its job:
    a **question** about photos ("تحب تشوف أنهي لون؟") leaves the customer
    something to answer, which is never dead air; and telling them a product
    has no photos is the honest answer the prompt asks for.
    """
    if not mentions_photo(text):
        return False
    if "؟" in text or "?" in text:
        return False
    return not _NO_IMAGES.search(text.lower())


#: The part of a product name that says what kind of garment it is rather than
#: which product it is. `Envy T-shirt` and `Cairokee T-shirt` share all of it,
#: so a reply mentioning «T-shirt» beside a photograph is not naming either --
#: and reading it as one made the rule fail a perfectly honest reply.
_GENERIC_NAME_WORDS = frozenset(
    {
        "tee", "tees", "t-shirt", "t-shirts", "tshirt", "shirt", "shirts",
        "hoodie", "hoodies", "sweatshirt", "sweatshirts", "crewneck",
        "sweatpant", "sweatpants", "joggers", "jacket", "jackets", "polo",
        "polos", "top", "tops", "zip", "zipup", "zip-through", "quarter-zip",
    }
)


def _latin_key(name: str) -> str:
    """The *distinctive* Latin word of a product name, lower-cased, or "".

    A reply names a product half the time in short form -- «صورة Lightweight
    الأسود» for `Lightweight Sweatpant` -- so the whole name is not a reliable
    handle. The longest Latin word that is not the garment kind is:
    `Lightweight`, `Ringer`, `Cairokee`.

    Two words are excluded and both matter. Anything under five letters
    (`Tee`, `Top`, `Zip`) appears inside other names; anything in
    `_GENERIC_NAME_WORDS` is shared by half the catalog, and a reply saying
    «T-shirt» has named a category, not a product.
    """
    words = [
        w
        for w in re.findall(r"[A-Za-z][A-Za-z-]*", name or "")
        if len(w) >= 5 and w.lower() not in _GENERIC_NAME_WORDS
    ]
    return max(words, key=len).lower() if words else ""


def product_names(history: list[dict]) -> dict[str, str]:
    """Every product name a tool result in this conversation has returned.

    Keyed by the name itself so the caller can report it back as the catalog
    spells it. This is what makes "claimed" a fact rather than a guess: a Latin
    word in a reply counts as a product reference only if a lookup in this same
    conversation actually produced that product.
    """
    names: dict[str, str] = {}

    def _note(value) -> None:
        if isinstance(value, str) and value.strip():
            names[value.strip()] = value.strip()

    for message in history:
        if message.get("role") != TOOL_RESULTS:
            continue
        for result in message.get("results") or []:
            content = result.get("content")
            if not isinstance(content, dict):
                continue
            _note(content.get("name"))
            if not content.get("name"):
                # A size chart's `title` is the chart's, not a product's --
                # "Oversized t-shirt" is shared by three of them. Only an
                # answer with no product name of its own speaks through it.
                _note(content.get("title"))
            for entry in content.get("products") or []:
                if isinstance(entry, dict):
                    _note(entry.get("name"))
    return names


def attached_names(labels: dict[str, dict]) -> set[str]:
    """The products this reply is actually carrying a photograph of.

    The size chart is deliberately not one of them: a chart is a picture of a
    table, and a reply that sends one has not shown the customer the garment.
    """
    found: set[str] = set()
    for label in (labels or {}).values():
        if not isinstance(label, dict):
            continue
        if str(label.get("label") or "").endswith("size chart"):
            continue
        name = label.get("name")
        if isinstance(name, str) and name.strip():
            found.add(name.strip())
    return found


def _product_photos(labels: dict[str, dict], attachments: list[str]) -> int:
    """How many of the attachments are photographs of a garment."""
    charts = sum(1 for path in attachments or [] if _is_chart(labels, path))
    return max(0, len(attachments or []) - charts)


def unbacked_claim(
    text: str,
    *,
    attachments: list[str],
    labels: dict[str, dict] | None = None,
    history: list[dict] | None = None,
    customer_sent_a_photo: bool = False,
) -> str:
    """Why this reply's words claim a photograph it is not sending, or "".

    Three ways a claim can be unbacked, weakest evidence last:

    1. nothing at all is attached and the sentence says a picture is coming;
    2. the sentence claims **two** ("الاتنين", "الصور") and one went;
    3. the sentence names a product, beside the word "photo", that this reply
       has no photograph of -- read per clause, so listing other products
       elsewhere in the same reply is not a claim about them.

    A customer who has just sent a photograph of their own exempts the whole
    check: every mention of a picture in that reply is about *theirs*.
    """
    if customer_sent_a_photo or not promises_images(text):
        return ""

    photos = _product_photos(labels or {}, attachments or [])
    charts = len(attachments or []) - photos
    if photos <= 0:
        if charts and not _garment_claim(text):
            # Every mention of a picture is about the chart, and the chart
            # is attached: the sentence is true.
            return ""
        return "the reply says a photo is coming and nothing is attached"

    known = product_names(history or [])
    attached = attached_names(labels or {})
    # A key that also belongs to a product this reply *did* send a photo of is
    # no evidence at all: `Cairokee T-shirt 2` and `Cairokee T-shirt` share
    # theirs, so a reply carrying the second's photo and naming it would read
    # as a claim about the first. When two names cannot be told apart, the
    # honest answer is to say nothing about either.
    attached_keys = {_latin_key(name) for name in attached}
    unattached_key: dict[str, str] = {}
    for name in known:
        key = _latin_key(name)
        if not key or name in attached or key in attached_keys:
            continue
        if key in unattached_key and unattached_key[key] != name:
            unattached_key[key] = ""  # two products, one key: ambiguous
            continue
        unattached_key.setdefault(key, name)
    unattached_key = {key: name for key, name in unattached_key.items() if name}

    for clause in _CLAUSE.split(text):
        lowered = clause.lower()
        if not _IMAGE_WORD.search(lowered) or _NO_IMAGES.search(lowered):
            continue

        # A clause that names the chart may count it: «دي صور الهودي وجدول
        # المقاسات» beside one photo and one chart is two pictures, as said.
        shown = photos + (charts if _CHART_WORD.search(lowered) else 0)
        if (_BOTH.search(clause) or _PLURAL_PHOTO.search(lowered)) and shown < 2:
            return (
                f"the reply claims more than one photo ({clause.strip()[:48]!r}) "
                f"and {photos} went out"
            )

        named = sorted(
            {
                name
                for key, name in unattached_key.items()
                if re.search(rf"(?<![A-Za-z]){re.escape(key)}", lowered)
            }
        )
        if named:
            return (
                f"the reply claims a photo of {', '.join(named)} and no photo of "
                "it is attached"
            )
    return ""


#: Blaming the customer's own phone, app or line for a send *we* failed. Every
#: one of these came out of, or sits one synonym away from, the audited reply
#: -- «ممكن تكون مشكلة في النت أو التطبيق — جرب اقفل الواتس وافتحه تاني» --
#: sent about a photograph the system had already recorded as undelivered.
#:
#: Matched only beside a photo word, so «النت عندنا بطيء» in some other context
#: is not caught: this is about the *explanation offered for a missing
#: picture*, not about the words themselves.
_BLAME = (
    "مشكلة في النت", "مشكله في النت", "النت عندك", "النت بتاعك", "الشبكة عندك",
    "الشبكه عندك", "مشكلة في التطبيق", "مشكله في التطبيق", "التطبيق عندك",
    "اقفل الواتس", "أقفل الواتس", "اقفل التطبيق", "اعمل ريستارت", "رستارت",
    "حدث التطبيق", "حدّث التطبيق", "التحديث", "امسح الكاش", "جرب من موبايل",
    "جرب تفتح", "جرب تقفل", "غير النت", "غيّر النت", "الواي فاي",
    "restart whatsapp", "restart the app", "check your internet",
    "your connection", "your network", "clear the cache", "update the app",
)


def blames_the_customer(text: str) -> str:
    """The phrase in which a reply blamed the customer for our failed send.

    When a customer says a photograph did not arrive, that is ground truth:
    the record says whether it went, and the record is ours to read. Telling
    them to restart WhatsApp is the shop asking the customer to debug a
    failure on the shop's side, and it is the worst answer available -- it is
    wrong, it is unanswerable, and it ends the conversation.
    """
    lowered = (text or "").lower()
    if not mentions_photo(lowered):
        return ""
    return next((phrase for phrase in _BLAME if phrase in lowered), "")


def undelivered(history: list[dict]) -> dict[str, str]:
    """Every photograph this conversation tried to send and the platform refused.

    `{path: what it was of}`, read from `undelivered_attachments` -- which the
    channel adapter writes onto the stored reply once the send has actually
    been attempted, since that is the only moment it is knowable (see
    `assistant/session.py::record_undelivered_attachments`). Until it existed a
    failed photo was indistinguishable from a delivered one from inside the
    turn, which is how the bot came to insist a picture had arrived while the
    alert queue already held the rejection.

    The label may be empty -- a photo the tool layer could not name is still a
    photo that did not arrive -- so callers report a count when it is.
    """
    failed: dict[str, str] = {}
    for message in history:
        if message.get("role") != ASSISTANT:
            continue
        stored = message.get("undelivered_attachments") or {}
        if isinstance(stored, list):  # tolerated: the shape before labels
            stored = {path: "" for path in stored if isinstance(path, str)}
        if not isinstance(stored, dict):
            continue
        for path, label in stored.items():
            if isinstance(path, str) and path:
                failed.setdefault(path, label if isinstance(label, str) else "")
    return failed


def undelivered_labels(history: list[dict]) -> list[str]:
    """The undelivered photographs, named the way the customer would know them.

    Never a file path: a path is meaningless to a customer and the prompt
    forbids writing one, so an unnamed failure contributes nothing here and the
    caller falls back to saying how many there were.
    """
    named: list[str] = []
    for label in undelivered(history).values():
        if label and label not in named:
            named.append(label)
    return named


#: Offering to show or send pictures of the garment: «تحب أوريك صور واحد
#: فيهم؟», «تحب تشوفه؟», «أبعتلك صورته؟». Not «تحب أشوفلك» (I check for you)
#: and not «تحب تشوف جدول المقاسات» -- a see-verb counts only with a photo
#: object or the garment as its pronoun.
_PHOTO_OBJECT = r"(?:صور|صورة|صوره|صورته|صورتها|صورهم|شكله|شكلها|شكلهم)"
_OFFER = re.compile(
    r"(?:تحب|تحبي|حابب|حابة|عايز|عايزة|ممكن)\s+(?:حضرتك\s+)?"
    r"(?:[أاإ]?(?:وريك|ورّيك|وريكي|وريلك|وريهولك|وريهالك|وريهملك)(?:\s+" + _PHOTO_OBJECT + r")?"
    r"|[أاإ]?بعت(?:لك|هولك|هالك|هملك)\s+" + _PHOTO_OBJECT +
    r"|تشوف(?:ه|ها|هم)(?![\u0600-\u06ff])"
    r"|تشوف\s+" + _PHOTO_OBJECT + r")"
)
_SENTENCE_END = re.compile(r"(?<=[.!؟?])\s+")


def offers_photos(text: str) -> str:
    """The phrase offering to show the garment, or ""."""
    match = _OFFER.search(text or "")
    return match.group(0) if match else ""


def offers_what_it_sends(text: str, attachments: list[str], photo_products: dict) -> str:
    """An offer to show photos in a reply that is already carrying garment
    photos, or "".

    instagram/1692370588503523, 2026-09-25 21:50:47: a list of three tees,
    two of their photos attached by the showcase, ending «تحب أوريك صور واحد
    فيهم؟». The words were written before the pictures were decided, so the
    reply asks permission for what it is already doing.
    """
    if not any(path in photo_products for path in attachments):
        return ""
    return offers_photos(text)


def without_the_offer(text: str) -> str:
    """`text` with the sentence that offers photos taken out; `text` itself
    if nothing else would be left."""
    lines = []
    for line in (text or "").splitlines():
        sentences = [part for part in _SENTENCE_END.split(line) if not _OFFER.search(part)]
        lines.append(" ".join(sentences).rstrip())
    trimmed = _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()
    return trimmed or text


_BLANK_RUN = re.compile(r"\n{3,}")
