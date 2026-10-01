"""What the shop actually says back to a public comment, per category.

Every string in this module is **hand-written and fixed**. Nothing here is
generated, and nothing here is ever handed to a model to rewrite -- the same
rule `assistant/comment_faq.py` and the old `PUBLIC_ACKS` follow, and the
reason is that the public surface is published under a live post where anyone
scrolling past reads it. A sentence a model chose is a sentence nobody
approved.

What changed is the *shape* of that rule, not the rule. One fixed line per
category read as a bot the moment two people asked the same thing: the shop
answered both with byte-identical text and the only difference was the quoted
comment. So a category no longer owns a line; it owns a **bank** of lines that
all say the same thing in different words, and one is picked per comment.

**Selection is deterministic, not random**, and that is deliberate. Meta
redelivers a webhook whenever it does not get a clean 200, and a retry has to
produce the *same* sentence -- `random.choice` would put a second, differently
worded reply under one customer's comment. So the pick is
`crc32(comment_id) % len(bank)`: stable for one comment, effectively
uncorrelated between comments. (`hash()` is salted per process and would pick
differently after any restart, which is the same bug with extra steps.)

The banks are sized so a collision between two comments is unlikely rather
than a coin flip -- the old three-line `PUBLIC_ACKS` gave two customers the
same line one time in three.

Tone is the shop's, not a friend's: short, Egyptian, respectful, plain.
It leads with the answer, uses no vocatives («يا وحش», «يا كبير») and no
emoji -- a public line under a live post is the brand talking, and it is
read by everybody scrolling past, not just the person who wrote the comment.
"""

from __future__ import annotations

import zlib

#: Categories whose public answer is a handoff -- the question needs a real
#: look at the catalog (which product, which size, which colour, is it in
#: stock), so it is answered in DM and the public line says so. These banks
#: deliberately do **not** claim the answer has already been sent: the DM that
#: follows is an opener, and a public line promising a price that the DM does
#: not contain is a broken promise published under a post.
_HANDOFF_BANKS: dict[str, tuple[str, ...]] = {
    "price": (
        "مستنيين رسالة حضرتك في الدايركت ونقولك السعر على طول.",
        "السعر والمقاسات المتاحة في الدايركت، ومستنيين رسالة حضرتك.",
        "السعر والتفاصيل في الدايركت.",
        "رسالة في الدايركت ونقولك بكام وأنهي مقاسات متاحة.",
        "الدايركت مفتوح، ونقولك السعر فوراً.",
        "الدايركت مفتوح لأي سؤال عن السعر، وتحت أمر حضرتك.",
    ),
    "availability": (
        "مستنيين رسالة حضرتك في الدايركت ونشوف المتاح دلوقتي.",
        "رسالة في الدايركت ونقولك لسه موجود ولا خلص.",
        "في الدايركت نتأكد من التوفر حالاً.",
        "الدايركت مفتوح، ونقولك أنهي مقاسات لسه متاحة.",
        "الدايركت مفتوح، نقولك المتوفر منه دلوقتي.",
        "التوفر بيتغير كل يوم، ومستنيين حضرتك في الدايركت نتأكد.",
    ),
    "size": (
        "مستنيين رسالة حضرتك في الدايركت ونظبط المقاس.",
        "رسالة في الدايركت ونقولك المقاسات المتاحة.",
        "جدول المقاسات كامل عندنا في الدايركت.",
        "الطول والوزن في الدايركت، ونرشح المقاس المناسب.",
        "في الدايركت نحدد المقاس المظبوط.",
        "الدايركت مفتوح، نشوف المقاس المناسب مع بعض.",
    ),
    "variant": (
        "مستنيين رسالة حضرتك في الدايركت ونوريك الألوان المتاحة.",
        "رسالة في الدايركت ونقولك فيه إيه تاني منه.",
        "في الدايركت نوريك باقي الألوان والموديلات.",
        "الصور كلها في الدايركت.",
        "الدايركت مفتوح، نوريك كل اللي عندنا منه.",
        "الألوان المتاحة كلها في الدايركت.",
    ),
    "product_info": (
        "مستنيين رسالة حضرتك في الدايركت ونقولك الخامة بالتفصيل.",
        "رسالة في الدايركت ونقولك تفاصيل الخامة.",
        "في الدايركت نقولك هو معمول من إيه بالظبط.",
        "الدايركت مفتوح لأي تفصيلة.",
        "الدايركت مفتوح لأي سؤال عن الخامة.",
        "تفاصيل القطعة كلها في الدايركت.",
    ),
    "other": (
        "مستنيين رسالة حضرتك في الدايركت.",
        "رسالة في الدايركت ونرد على طول.",
        "في الدايركت نساعد في أي حاجة.",
        "الدايركت مفتوح، وتحت أمر حضرتك.",
        "الدايركت مفتوح لأي سؤال.",
        "تحت أمر حضرتك في الدايركت.",
    ),
}

#: `order_status` is a handoff too, but it is answered in a different voice: the
#: person is waiting on something they already paid for, so the public line
#: reassures before it redirects. It never states where the order is -- nobody
#: has looked yet.
_ORDER_STATUS = (
    "بعتنا رسالة في الدايركت عشان نتابع الأوردر.",
    "مستنيين حضرتك في الدايركت ونتابع الأوردر حالاً.",
    "جايين في الدايركت نشوف الأوردر فوراً.",
    "رقم الأوردر في الدايركت ونتابعه فوراً.",
    "في الدايركت نطمن حضرتك على الأوردر.",
)

#: A real customer with a real problem, said in public. Admits nothing --
#: nobody has read the order yet, and an apology here is the shop confessing
#: to something that may not have happened -- but it must never read as
#: dismissive either.
_COMPLAINT = (
    "بعتنالك في الدايركت عشان نظبطها فوراً.",
    "آسفين على أي تعب، مستنيين حضرتك في الدايركت ونحلها حالاً.",
    "جايين في الدايركت نشوف الموضوع ده على طول.",
    "وصلنا الكلام، وفي الدايركت نظبطها فوراً.",
    "في الدايركت هنتابعها مع حضرتك لحد ما تتحل.",
)

#: A hater, not a customer. This bank exists because the alternative shipped
#: for months and was *silence*: a bad word under a live post, seen by
#: everyone scrolling, with the shop saying nothing. One short, calm,
#: un-defensive line reads better to the hundred people reading than to the
#: one person who wrote it -- which is who it is actually for. It never
#: argues, never justifies, and never matches the tone it is answering.
#:
#: And it **never mentions the DM**, because `negative` does not open one
#: (`_ACTIONS` in
#: `assistant/channels/instagram.py`: chasing a critic into their inbox is how
#: a bad comment becomes a screenshot). These lines used to end in
#: "تحت أمرك في الدايركت", which published an invitation nobody was going to
#: honour: a customer who accepted it wrote into a thread the shop had not
#: opened and had no alert pointing at. So the line acknowledges that the
#: comment was read, and stops there -- what happens next belongs to the
#: staff member the `negative_comment` alert wakes up, not to a promise made
#: under the post.
_NEGATIVE = (
    "وصلني رأيك، وشكراً إنك قولتهولنا.",
    "كلامك مسموع، ومقدرين صراحتك.",
    "وصلنا كلامك، ورأيك يهمنا.",
    "أخدنا ملاحظتك بجدية.",
    "شكراً لصراحتك، وإحنا بنتعلم من كل رأي.",
)

#: A compliment. No DM, no model call -- just the thank-you the old "like"
#: was always meant to be (Instagram has no API for liking a comment; see
#: the note where `like_comment` used to live).
_POSITIVE = (
    "منورين رحلة.",
    "شكراً على ذوق حضرتك.",
    "نورتنا.",
    "ده ذوق حضرتك.",
    "شكراً للكلام الجميل.",
    "نورت البوست.",
    "سعداء إنه عجبك.",
    "كلك ذوق.",
)

#: Someone tagging a friend and saying nothing else ("@سارة بصي دي"). The
#: person to win over is the *friend* who is about to get the notification,
#: not the tagger -- so the line is light and welcoming and never a sales
#: pitch. Previously this got nothing at all.
_TAG_FRIEND = (
    "نورتوا.",
    "اختيار جميل.",
    "أهلاً بيكم.",
    "نورتونا الاتنين.",
    "ذوق من ذوق.",
)

_BANKS: dict[str, tuple[str, ...]] = {
    **_HANDOFF_BANKS,
    "order_status": _ORDER_STATUS,
    "complaint": _COMPLAINT,
    "negative": _NEGATIVE,
    "positive": _POSITIVE,
    "tag_friend": _TAG_FRIEND,
}

#: The DM that follows a handoff. Its job is to open a real conversation, so
#: it names what they asked about and invites the one detail the shop needs to
#: answer properly -- it is honest about being an opener rather than
#: pretending to be the answer. `{comment}` is the customer's own words, and
#: it is the only substitution any of these take.
_DM_OPENERS: dict[str, tuple[str, ...]] = {
    "price": (
        "شفنا كومنت حضرتك على البوست:\n«{comment}»\nممكن نعرف أنهي قطعة بالظبط؟ ونقولك سعرها والمقاسات المتاحة.",  # noqa: E501
        "أهلاً بحضرتك. شفنا سؤالك على البوست «{comment}» — اسم القطعة أو صورتها ونقولك السعر فوراً.",
        "وصلنا كومنتك «{comment}». ممكن نعرف أنهي موديل ولون؟ ونقولك بكام وأنهي مقاسات متاحة.",
        "نورتنا. بخصوص «{comment}» — محتاجين بس نعرف القطعة ونبعتلك السعر والتفاصيل على طول.",
    ),
    "availability": (
        "شفنا كومنت حضرتك على البوست:\n«{comment}»\nممكن نعرف أنهي قطعة ومقاس؟ ونشوف المتاح حالاً.",
        "أهلاً بحضرتك. بخصوص «{comment}» — ممكن نعرف الموديل واللون؟ ونتأكد من التوفر فوراً.",
        "وصلنا سؤالك «{comment}». ممكن نعرف المقاس المطلوب؟ ونقولك موجود ولا لأ.",
        "نورتنا. «{comment}» — ممكن نعرف القطعة؟ ونشوف اللي لسه متاح منها.",
    ),
    "size": (
        "شفنا كومنت حضرتك على البوست:\n«{comment}»\nممكن نعرف الطول والوزن؟ ونرشح المقاس المظبوط.",
        "أهلاً بحضرتك. بخصوص «{comment}» — جدول المقاسات كامل عندنا، ممكن نعرف القطعة؟",
        "وصلنا سؤالك «{comment}». ممكن نعرف المقاس المعتاد؟ ونقولك المقاس المناسب.",
        "نورتنا. «{comment}» — ممكن نعرف الموديل؟ ونبعت المقاسات والقياسات بالسنتي.",
    ),
    "variant": (
        "شفنا كومنت حضرتك على البوست:\n«{comment}»\nممكن نعرف اللون المطلوب؟ ونوريك المتاح.",
        "أهلاً بحضرتك. بخصوص «{comment}» — عندنا أكتر من لون وموديل، ممكن نعرف الأنسب؟",
        "وصلنا سؤالك «{comment}». ممكن نعرف القطعة؟ ونبعتلك كل الألوان المتاحة منها.",
        "نورتنا. «{comment}» — ممكن نعرف اللي عجبك؟ ونوريك اللي شبهه عندنا.",
    ),
    "product_info": (
        "شفنا كومنت حضرتك على البوست:\n«{comment}»\nممكن نعرف أنهي قطعة؟ ونقولك خامتها بالتفصيل.",
        "أهلاً بحضرتك. بخصوص «{comment}» — ممكن نعرف الموديل؟ ونقولك الخامة والتفاصيل.",
        "وصلنا سؤالك «{comment}». ممكن نعرف القطعة؟ ونقولك معمولة من إيه وإزاي تتغسل.",
        "نورتنا. «{comment}» — أي تفصيلة، إحنا موجودين.",
    ),
    "order_status": (
        "شفنا كومنت حضرتك على البوست:\n«{comment}»\nممكن رقم الأوردر أو رقم التليفون؟ ونتابعه حالاً.",
        "أهلاً بحضرتك. بخصوص «{comment}» — ممكن رقم الأوردر؟ ونشوف هو فين دلوقتي.",
        "وصلنا كلامك «{comment}». ممكن بيانات الأوردر؟ ونتابعه مع حضرتك فوراً.",
        "آسفين على القلق. «{comment}» — ممكن رقم الأوردر؟ ونطمن حضرتك عليه.",
    ),
    "complaint": (
        "شفنا كومنت حضرتك على البوست:\n«{comment}»\nممكن نعرف تفاصيل اللي حصل؟ وهنظبطها فوراً.",
        "آسفين جداً على التعب. «{comment}» — ممكن رقم الأوردر؟ وهنحلها حالاً.",
        "وصلنا كلامك «{comment}». ممكن التفاصيل؟ وإحنا متابعين مع حضرتك لحد ما تتحل.",
        "آسفين على اللي حصل. «{comment}» — ممكن البيانات؟ وهنشوفها على طول.",
    ),
    "other": (
        "شفنا كومنت حضرتك على البوست:\n«{comment}»\nإزاي نقدر نساعد حضرتك؟",
        "أهلاً بحضرتك. وصلنا «{comment}» — ممكن نعرف المطلوب بالظبط؟ ونساعد على طول.",
        "نورتنا. بخصوص «{comment}» — أي سؤال، إحنا موجودين.",
        "وصلنا كومنتك «{comment}». نقدر نساعد حضرتك بإيه؟",
    ),
}


def _pick(bank: tuple[str, ...], comment_id: str) -> str:
    """One line out of a bank, stable for one comment id.

    Deterministic on purpose -- see the module docstring: a Meta redelivery
    must reproduce the same sentence, or the customer gets a second reply
    worded differently under the same comment.
    """
    return bank[zlib.crc32(comment_id.encode("utf-8")) % len(bank)]


def public_reply(category: str, comment_id: str) -> str | None:
    """The public line for this category, or None when it gets no public reply.

    `spam` is the one category that deliberately returns None: a public answer
    to a scam bot is the shop amplifying it to everyone reading the post, and
    the bot cannot tell a scammer it has embarrassed from one it has helped.
    Spam is answered to *staff*, in the queue, which is where someone can act
    on it.
    """
    bank = _BANKS.get(category)
    return _pick(bank, comment_id) if bank else None


def dm_opener(category: str, comment_id: str, comment_text: str) -> str:
    """The private reply that opens the thread.

    Falls back to the `other` bank for any category without one of its own, so
    a category added to the classifier before its copy is written still opens
    a real conversation instead of raising.
    """
    bank = _DM_OPENERS.get(category) or _DM_OPENERS["other"]
    return _pick(bank, comment_id).format(comment=comment_text[:200])


def bank_size(category: str) -> int:
    """How many public variants a category has. For tests and for the docs."""
    return len(_BANKS.get(category, ()))


# ---------------------------------------------------------------------------
# Rehla: short comments and product-aware DMs
# ---------------------------------------------------------------------------

#: A comment with (almost) nothing to answer -- "Hm", "🔥", "." -- still gets
#: a warm public line instead of silence. Picked like every other bank.
_SHORT_PUBLIC: tuple[str, ...] = (
    "منورين 🤍 أي تفاصيل، رسالة على الخاص وهنرد على طول",
    "نورتنا 🤍 بعتنالك رسالة، ولأي حاجة إحنا موجودين",
    "ميرسي على الكومنت 🤍 السعر والمقاسات على الخاص",
    "منورين رحلة 🤍 أي سؤال، رسالة على الخاص وهنرد",
    "حبينا الكومنت 🤍 تفاصيل أكتر على الخاص",
)

#: The one DM a short comment opens with when the post names no single product.
SHORT_DM = "أهلاً بحضرتك في رحلة 🤍 نقدر نساعد في إيه؟"

_TYPE_AR = {"Tops": "توب", "T-Shirts": "تيشيرت", "Pants": "بنطلون", "Caps": "كاب"}


def short_public(comment_id: str) -> str:
    return _pick(_SHORT_PUBLIC, comment_id)


def _egp(value) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def _garment(product: dict) -> str:
    if product.get("category") == "Hoodies & Jackets":
        return "جاكيت" if "jacket" in (product.get("style") or []) else "هودي"
    return _TYPE_AR.get(product.get("category") or "", "قطعة")


def product_dm(product: dict, *, greeting: bool = False) -> str:
    """The answer a comment on a post about ONE known product gets in its DM:
    price, sizes and colours from the catalog summary (`catalog._product_summary`),
    never a model. Lines open with an Arabic word so they lay out right."""
    lines = []
    if greeting:
        lines.append("أهلاً بحضرتك في رحلة 🤍")
    name = f"{_garment(product)} {product['name']}"
    if not product.get("any_in_stock"):
        lines.append(f"{name} خلصانة حاليًا للأسف.")
        lines.append("نوريك حاجة شبهها متاحة؟")
        return "\n".join(lines)
    price_from, price_to = product.get("price_from"), product.get("price_to")
    price = f"من {_egp(price_from)}" if price_from != price_to else _egp(price_from)
    line = f"{name} — السعر {price} جنيه"
    original = product.get("original_price_to")
    if product.get("on_sale") and original and float(original) > float(price_to or 0):
        line += f" بدل {_egp(original)}"
    lines.append(line)
    sizes = product.get("in_stock_sizes") or []
    if sizes and sizes != ["One Size"]:
        lines.append("المقاسات المتاحة: " + " و ".join(sizes))
    colors = product.get("in_stock_colors") or []
    if colors:
        lines.append("الألوان المتاحة: " + " و ".join(colors))
    lines.append("ممكن نعرف المقاس واللون المطلوبين؟")
    return "\n".join(lines)
