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
        "كلمني في الدايركت وأقولك السعر على طول.",
        "ابعتيلي في الدايركت وأقولك السعر والمقاسات المتاحة.",
        "السعر والتفاصيل في الدايركت، ابعتلنا.",
        "تعالى الدايركت وأقولك بكام وأنهي مقاسات متاحة.",
        "رد عليا في الدايركت وأقولك السعر فوراً.",
        "الدايركت مفتوح، اسألي عن السعر وأنا تحت أمرك.",
    ),
    "availability": (
        "كلمني في الدايركت وأشوفلك المتاح دلوقتي.",
        "ابعتيلي في الدايركت وأقولك لسه موجود ولا خلص.",
        "تعالى الدايركت وأتأكدلك من التوفر حالاً.",
        "رد عليا في الدايركت وأقولك أنهي مقاسات لسه متاحة.",
        "الدايركت مفتوح، أقولك المتوفر منه دلوقتي.",
        "التوفر بيتغير كل يوم، كلمني في الدايركت وأتأكدلك.",
    ),
    "size": (
        "كلمني في الدايركت وأظبطلك المقاس.",
        "ابعتيلي في الدايركت وأقولك المقاسات المتاحة.",
        "جدول المقاسات كامل عندنا، تعالى الدايركت.",
        "قوليلي طولك ووزنك في الدايركت وأرشحلك المقاس.",
        "رد عليا في الدايركت ونحدد المقاس المظبوط.",
        "الدايركت مفتوح، نشوف مقاسك مع بعض.",
    ),
    "variant": (
        "كلمني في الدايركت وأوريك الألوان المتاحة.",
        "ابعتيلي في الدايركت وأقولك فيه إيه تاني منه.",
        "تعالى الدايركت وأوريك باقي الألوان والموديلات.",
        "رد عليا في الدايركت وأبعتلك الصور كلها.",
        "الدايركت مفتوح، أوريك كل اللي عندنا منه.",
        "الألوان المتاحة كلها في الدايركت، ابعتلنا.",
    ),
    "product_info": (
        "كلمني في الدايركت وأقولك الخامة بالتفصيل.",
        "ابعتيلي في الدايركت وأقولك تفاصيل الخامة.",
        "تعالى الدايركت وأقولك هو معمول من إيه بالظبط.",
        "رد عليا في الدايركت وأجاوبك على أي تفصيلة.",
        "الدايركت مفتوح لأي سؤال عن الخامة.",
        "تفاصيل القطعة كلها في الدايركت.",
    ),
    "other": (
        "كلمني في الدايركت وأنا تحت أمرك.",
        "ابعتيلي في الدايركت وأجاوبك على طول.",
        "تعالى الدايركت ونظبط اللي محتاجه.",
        "رد عليا في الدايركت وأساعدك.",
        "الدايركت مفتوح، اسألي عن أي حاجة.",
        "تحت أمرك في الدايركت.",
    ),
}

#: `order_status` is a handoff too, but it is answered in a different voice: the
#: person is waiting on something they already paid for, so the public line
#: reassures before it redirects. It never states where the order is -- nobody
#: has looked yet.
_ORDER_STATUS = (
    "بعتلك في الدايركت عشان نتابع الأوردر.",
    "كلمني في الدايركت وأتابعلك الأوردر حالاً.",
    "جاييلك في الدايركت أشوفلك الأوردر فوراً.",
    "ابعتيلي رقم الأوردر في الدايركت وأتابعهولك.",
    "تعالى الدايركت وأطمنك على أوردرك.",
)

#: A real customer with a real problem, said in public. Admits nothing --
#: nobody has read the order yet, and an apology here is the shop confessing
#: to something that may not have happened -- but it must never read as
#: dismissive either.
_COMPLAINT = (
    "بعتنالك في الدايركت عشان نظبطها فوراً.",
    "آسفين على أي تعب، كلمني في الدايركت ونحلها حالاً.",
    "جاييلك في الدايركت نشوف الموضوع ده على طول.",
    "وصلني كلامك، تعالى الدايركت ونظبطهالك فوراً.",
    "كلمني في الدايركت وهنتابعها معاك لحد ما تتحل.",
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
    "منور.",
    "شكراً على ذوقك.",
    "نورتنا.",
    "ده ذوقك.",
    "شكراً لكلامك الجميل.",
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
        "شفت كومنتك على البوست:\n«{comment}»\nقوليلي أنهي قطعة بالظبط وأقولك سعرها والمقاسات المتاحة.",
        "أهلاً بيكي. شفت سؤالك على البوست «{comment}» — قوليلي اسم القطعة أو ابعتيلي صورتها وأقولك السعر فوراً.",  # noqa: E501
        "وصلني كومنتك «{comment}». قوليلي أنهي موديل ولون وأقولك بكام وأنهي مقاسات متاحة.",
        "نورتنا. بخصوص «{comment}» — حدّديلي القطعة وأبعتلك السعر والتفاصيل على طول.",
    ),
    "availability": (
        "شفت كومنتك على البوست:\n«{comment}»\nقوليلي أنهي قطعة ومقاس وأشوفلك المتاح حالاً.",
        "أهلاً بيكي. بخصوص «{comment}» — قوليلي الموديل واللون وأتأكدلك من التوفر فوراً.",
        "وصلني سؤالك «{comment}». حدّديلي المقاس اللي بتدور عليه وأقولك موجود ولا لأ.",
        "نورتنا. «{comment}» — قوليلي القطعة وأشوفلك اللي لسه متاح منها.",
    ),
    "size": (
        "شفت كومنتك على البوست:\n«{comment}»\nقوليلي طولك ووزنك وأرشحلك المقاس المظبوط.",
        "أهلاً بيكي. بخصوص «{comment}» — عندي جدول المقاسات كامل، قوليلي القطعة وأظبطهولك.",
        "وصلني سؤالك «{comment}». قوليلي مقاسك المعتاد وأقولك يمشي ولا تكبّر.",
        "نورتنا. «{comment}» — حدّديلي الموديل وأبعتلك المقاسات والقياسات بالسنتي.",
    ),
    "variant": (
        "شفت كومنتك على البوست:\n«{comment}»\nقوليلي بتدوري على أنهي لون وأوريك المتاح.",
        "أهلاً بيكي. بخصوص «{comment}» — عندنا أكتر من لون وموديل، قوليلي اللي يناسبك وأرشحلك.",
        "وصلني سؤالك «{comment}». قوليلي القطعة وأبعتلك كل الألوان المتاحة منها.",
        "نورتنا. «{comment}» — حدّديلي اللي عجبك وأوريك اللي شبهه عندنا.",
    ),
    "product_info": (
        "شفت كومنتك على البوست:\n«{comment}»\nقوليلي أنهي قطعة وأقولك خامتها بالتفصيل.",
        "أهلاً بيكي. بخصوص «{comment}» — حدّديلي الموديل وأقولك الخامة والتفاصيل.",
        "وصلني سؤالك «{comment}». قوليلي القطعة وأقولك معمولة من إيه وإزاي تغسلها.",
        "نورتنا. «{comment}» — اسألي عن أي تفصيلة وأنا أجاوبك.",
    ),
    "order_status": (
        "شفت كومنتك على البوست:\n«{comment}»\nابعتيلي رقم الأوردر أو رقم تليفونك وأتابعهولك حالاً.",
        "أهلاً بيكي. بخصوص «{comment}» — قوليلي رقم الأوردر وأشوفلك هو فين دلوقتي.",
        "وصلني كلامك «{comment}». ابعتيلي بياناتك وأتابع الأوردر معاكي فوراً.",
        "آسفين على القلق. «{comment}» — قوليلي رقم الأوردر وأطمنك عليه.",
    ),
    "complaint": (
        "شفت كومنتك على البوست:\n«{comment}»\nقوليلي تفاصيل اللي حصل وهنظبطها فوراً.",
        "آسفين جداً على التعب. «{comment}» — ابعتيلي رقم الأوردر وهنحلها حالاً.",
        "وصلني كلامك «{comment}». احكيلي بالتفصيل وأنا متابعة معاكي لحد ما تتحل.",
        "آسفين على اللي حصل. «{comment}» — قوليلي بياناتك وهنشوفها على طول.",
    ),
    "other": (
        "شفت كومنتك على البوست:\n«{comment}»\nقوليلي محتاجة إيه وأنا تحت أمرك.",
        "أهلاً بيكي. وصلني «{comment}» — قوليلي محتاجة إيه بالظبط وأساعدك.",
        "نورتنا. بخصوص «{comment}» — اسألي عن أي حاجة وأنا موجود.",
        "وصلني كومنتك «{comment}». قوليلي أقدر أساعدك بإيه.",
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
    "منورانا 🤍 ابعتيلنا رسالة لو عايزة تعرفي أي تفاصيل",
    "نورتينا 🤍 بعتنالك رسالة، لو محتاجة أي حاجة إحنا موجودين",
    "ميرسي على كومنتك 🤍 لو عايزة سعر أو مقاس ابعتيلنا على الخاص",
    "منورة رحلة 🤍 أي سؤال ابعتيلنا رسالة وهنرد عليكي",
    "حبينا كومنتك 🤍 ابعتيلنا لو حابة تعرفي تفاصيل أكتر",
)

#: The one DM a short comment opens with when the post names no single product.
SHORT_DM = "أهلًا بيكي في رحلة 🤍 تحبي أساعدك في إيه؟"

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
        lines.append("أهلًا بيكي في رحلة 🤍")
    name = f"{_garment(product)} {product['name']}"
    if not product.get("any_in_stock"):
        lines.append(f"{name} خلصانة حاليًا للأسف.")
        lines.append("تحبي أوريكي حاجة شبهها متاحة؟")
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
    lines.append("تحبي أحجزلك مقاس ولون إيه؟")
    return "\n".join(lines)
