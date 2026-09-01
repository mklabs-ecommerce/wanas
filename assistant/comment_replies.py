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

Tone follows the shop's own captions: short, Egyptian, warm, one emoji at the
end, 🖤/🤍/✨ rather than a wall of them.
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
        "كلمني في الدايركت وأقولك السعر على طول 🖤",
        "ابعتلي في الدايركت وأقولك سعره والمقاسات المتاحة 🤍",
        "جاييلك في الدايركت نظبطلك السعر ✨",
        "تعالى الدايركت وأقولك بكام وأنهي مقاسات فاضلة 🖤",
        "رد عليا في الدايركت وأقولك السعر فوراً 🤍",
        "الدايركت مفتوح، اسألني عن السعر وأنا تحت أمرك 🖤",
    ),
    "availability": (
        "كلمني في الدايركت أشوفلك المتاح دلوقتي 🖤",
        "ابعتلي في الدايركت وأقولك لسه موجود ولا خلص 🤍",
        "تعالى الدايركت أتأكدلك من التوفر حالاً ✨",
        "رد عليا في الدايركت وأشوفلك أنهي مقاسات لسه فاضلة 🖤",
        "الدايركت مفتوح، أقولك المتوفر منه دلوقتي 🤍",
        "جاييلك في الدايركت أطمنك على التوفر 🖤",
    ),
    "size": (
        "كلمني في الدايركت وأظبطلك المقاس 🖤",
        "ابعتلي في الدايركت وأقولك المقاسات المتاحة 🤍",
        "تعالى الدايركت ومعايا جدول المقاسات كامل ✨",
        "قولّي طولك ووزنك في الدايركت وأرشحلك المقاس 🖤",
        "رد عليا في الدايركت نختار المقاس المظبوط 🤍",
        "الدايركت مفتوح، نشوف مقاسك مع بعض 🖤",
    ),
    "variant": (
        "كلمني في الدايركت وأوريك الألوان المتاحة 🖤",
        "ابعتلي في الدايركت وأقولك فيه إيه تاني منه 🤍",
        "تعالى الدايركت أوريك باقي الألوان والموديلات ✨",
        "رد عليا في الدايركت وأبعتلك الصور كلها 🖤",
        "الدايركت مفتوح، أوريك كل اللي عندنا منه 🤍",
        "جاييلك في الدايركت بكل الخيارات 🖤",
    ),
    "product_info": (
        "كلمني في الدايركت وأقولك الخامة بالتفصيل 🖤",
        "ابعتلي في الدايركت وأحكيلك عن الخامة والتفاصيل 🤍",
        "تعالى الدايركت وأقولك هو معمول من إيه بالظبط ✨",
        "رد عليا في الدايركت وأجاوبك على أي تفصيلة 🖤",
        "الدايركت مفتوح لأي سؤال عن الخامة 🤍",
        "جاييلك في الدايركت بكل تفاصيل القطعة 🖤",
    ),
    "other": (
        "كلمني في الدايركت وأنا تحت أمرك 🖤",
        "ابعتلي في الدايركت وأجاوبك على طول 🤍",
        "تعالى الدايركت ونظبط اللي محتاجه ✨",
        "رد عليا في الدايركت وأساعدك 🖤",
        "الدايركت مفتوح، اسأل عن أي حاجة 🤍",
        "جاييلك في الدايركت 🖤",
    ),
}

#: `order_status` is a handoff too, but it is answered in a different voice: the
#: person is waiting on something they already paid for, so the public line
#: reassures before it redirects. It never states where the order is -- nobody
#: has looked yet.
_ORDER_STATUS = (
    "بعتلك في الدايركت نتابع الأوردر مع بعض 🖤",
    "كلمني في الدايركت وأتابعلك الأوردر حالاً 🤍",
    "جاييلك في الدايركت أشوفلك الأوردر فوراً ✨",
    "ابعتلي رقم الأوردر في الدايركت وأتابعهولك 🖤",
    "تعالى الدايركت وأطمنك على أوردرك 🤍",
)

#: A real customer with a real problem, said in public. Admits nothing --
#: nobody has read the order yet, and an apology here is the shop confessing
#: to something that may not have happened -- but it must never read as
#: dismissive either.
_COMPLAINT = (
    "بعتنالك في الدايركت عشان نظبطها فوراً 🖤",
    "آسفين على أي تعب، كلمني في الدايركت ونحلها حالاً 🖤",
    "جاييلك في الدايركت نشوف الموضوع ده على طول 🤍",
    "وصلني كلامك، تعالى الدايركت ونظبطهالك فوراً 🖤",
    "كلمني في الدايركت وهنتابعها معاك لحد ما تتحل 🤍",
)

#: A hater, not a customer. This bank exists because the alternative shipped
#: for months and was *silence*: a bad word under a live post, seen by
#: everyone scrolling, with the shop saying nothing. One short, calm,
#: un-defensive line reads better to the hundred people reading than to the
#: one person who wrote it -- which is who it is actually for. It never
#: argues, never justifies, and never matches the tone it is answering.
_NEGATIVE = (
    "رأيك يهمنا، ولو فيه حاجة نظبطها كلمنا في الدايركت 🖤",
    "أسف إن ده انطباعك، إحنا تحت أمرك في الدايركت 🤍",
    "وصلني رأيك، ولو حابب تقولنا أكتر إحنا موجودين 🖤",
    "شكراً لصراحتك، وأي ملاحظة تحت أمرك في الدايركت 🤍",
    "نحترم رأيك، ولو فيه مشكلة فعلية يشرفنا نحلها 🖤",
)

#: A compliment. No DM, no model call -- just the thank-you the old "like"
#: was always meant to be (Instagram has no API for liking a comment; see
#: the note where `like_comment` used to live).
_POSITIVE = (
    "منور 🖤",
    "تسلم يا وحش 🔥",
    "نورتنا 🖤",
    "ده ذوقك 🤍",
    "شكراً يا كبير 🖤",
    "نورت البوست ✨",
    "تسلم يا فنان 🤍",
    "كلك ذوق 🖤",
)

#: Someone tagging a friend and saying nothing else ("@سارة بصي دي"). The
#: person to win over is the *friend* who is about to get the notification,
#: not the tagger -- so the line is light and welcoming and never a sales
#: pitch. Previously this got nothing at all.
_TAG_FRIEND = (
    "نورتوا 🖤",
    "تحفة الاختيار ده 🤍",
    "أهلاً بيكم 🖤",
    "نورتونا الاتنين ✨",
    "ذوق من ذوق 🤍",
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
        "شفت كومنتك على البوست 👀\n«{comment}»\nقولّي أنهي قطعة بالظبط وأقولك سعرها والمقاسات المتاحة 🖤",
        "أهلاً بيك 🖤 شفت سؤالك على البوست «{comment}» — قولّي اسم القطعة أو ابعتلي صورتها وأقولك السعر فوراً",
        "وصلني كومنتك «{comment}» ✨ قولّي أنهي موديل ولون وأنا أقولك بكام وأنهي مقاسات فاضلة",
        "نورتنا 🤍 بخصوص «{comment}» — حدّدلي القطعة وأبعتلك السعر والتفاصيل على طول",
    ),
    "availability": (
        "شفت كومنتك على البوست 👀\n«{comment}»\nقولّي أنهي قطعة ومقاس وأشوفلك المتاح حالاً 🖤",
        "أهلاً بيك 🖤 بخصوص «{comment}» — قولّي الموديل واللون وأتأكدلك من التوفر فوراً",
        "وصلني سؤالك «{comment}» ✨ حدّدلي المقاس اللي بتدور عليه وأقولك موجود ولا لأ",
        "نورتنا 🤍 «{comment}» — قولّي القطعة وأنا أشوفلك اللي لسه فاضل منها",
    ),
    "size": (
        "شفت كومنتك على البوست 👀\n«{comment}»\nقولّي طولك ووزنك وأرشحلك المقاس المظبوط 🖤",
        "أهلاً بيك 🖤 بخصوص «{comment}» — عندي جدول المقاسات كامل، قولّي القطعة وأظبطك",
        "وصلني سؤالك «{comment}» ✨ قولّي مقاسك المعتاد وأقولك يمشي ولا تكبّر",
        "نورتنا 🤍 «{comment}» — حدّدلي الموديل وأبعتلك المقاسات والقياسات بالسنتي",
    ),
    "variant": (
        "شفت كومنتك على البوست 👀\n«{comment}»\nقولّي بتدور على أنهي لون وأوريك المتاح 🖤",
        "أهلاً بيك 🖤 بخصوص «{comment}» — عندنا أكتر من لون وموديل، قولّي ذوقك وأرشحلك",
        "وصلني سؤالك «{comment}» ✨ قولّي القطعة وأبعتلك كل الألوان المتاحة منها",
        "نورتنا 🤍 «{comment}» — حدّدلي اللي عجبك وأوريك اللي شبهه عندنا",
    ),
    "product_info": (
        "شفت كومنتك على البوست 👀\n«{comment}»\nقولّي أنهي قطعة وأقولك خامتها بالتفصيل 🖤",
        "أهلاً بيك 🖤 بخصوص «{comment}» — حدّدلي الموديل وأحكيلك عن الخامة والتفصيل",
        "وصلني سؤالك «{comment}» ✨ قولّي القطعة وأقولك معمولة من إيه وإزاي تغسلها",
        "نورتنا 🤍 «{comment}» — اسأل عن أي تفصيلة وأنا أجاوبك",
    ),
    "order_status": (
        "شفت كومنتك على البوست 👀\n«{comment}»\nابعتلي رقم الأوردر أو رقم تليفونك وأتابعهولك حالاً 🖤",
        "أهلاً بيك 🖤 بخصوص «{comment}» — قولّي رقم الأوردر وأشوفلك هو فين دلوقتي",
        "وصلني كلامك «{comment}» ✨ ابعتلي بياناتك وأتابع الأوردر معاك فوراً",
        "آسفين على القلق 🤍 «{comment}» — قولّي رقم الأوردر وأطمنك عليه",
    ),
    "complaint": (
        "شفت كومنتك على البوست 👀\n«{comment}»\nقولّي تفاصيل اللي حصل وهنظبطها فوراً 🖤",
        "آسفين جداً على التعب 🖤 «{comment}» — ابعتلي رقم الأوردر وهنحلها حالاً",
        "وصلني كلامك «{comment}» 🤍 احكيلي بالتفصيل وأنا متابع معاك لحد ما تتحل",
        "أسف على اللي حصل 🖤 «{comment}» — قولّي بياناتك وهنشوفها على طول",
    ),
    "other": (
        "شفت كومنتك على البوست 👀\n«{comment}»\nقولّي وأنا تحت أمرك 🖤",
        "أهلاً بيك 🖤 وصلني «{comment}» — قولّي محتاج إيه بالظبط وأساعدك",
        "نورتنا ✨ بخصوص «{comment}» — اسأل عن أي حاجة وأنا موجود",
        "وصلني كومنتك «{comment}» 🤍 قولّي أقدر أساعدك بإيه",
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
