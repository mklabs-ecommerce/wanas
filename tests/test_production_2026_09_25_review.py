"""The 25 September test conversations, replayed turn by turn.

Production, deployed 92475b1, three conversations from the same tester:

* ``whatsapp/201021233010`` 17:51-17:52 UTC -- the last WhatsApp
  conversation in production;
* ``instagram_dm/1692370588503523`` 19:13 and 21:50-21:58 UTC -- the same
  person (he gave 01021233010 as his phone), ending in order #1042;
* ``whatsapp/201067177129`` 07:43-16:35 UTC -- the size-chart saga, most of
  it on the deploys before 92475b1.

Every message, tool call and reply below is copied from the stored
transcript. The review that explains each one is
``docs/REVIEW_2026-09-25_HALLUCINATIONS.md``; the sections here follow its
root causes, and every test in this file failed on 92475b1.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from assistant import agent, messages as msg, showcase
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.tools.base import ToolContext, call_tool
from assistant.tools.catalog_tools import asked_about_sizing
from domain.models import Client, Product, Variant
from domain.services import catalog, identities

CDN = "https://cdn.shopify.com/s/files/1/0763/1296/9372/files/"
PLAIN = "oversized-plain-t-shirt-4"
PLAIN_CHART = CDN + "size_chart_8382c158-0c7b-4828-aea5-1014f5f6cea7.png?v=1788039089"
PLAIN_PHOTOS = {
    "black": CDN + "black.png?v=1788039093",
    "white": CDN + "white.png?v=1788039093",
    "navy": CDN + "navy.png?v=1788039093",
}

#: What `media.photo_context` wrote in front of the Instagram turns -- the
#: runtime's note about the customer's photo, stored in the customer's message.
PHOTO_NOTE = (
    "[الزبون بعت صورة] قراءة آلية للصورة: أقرب منتج عندنا هو oversized plain t-shirt. "
    "اتأكد بنفسك بالأدوات قبل ما تقول أي حاجة عن السعر أو المقاسات أو التوفر، "
    "واسأل الزبون لو ده اللي هو قاصده."
)


def _call(name: str, **arguments) -> ModelReply:
    return ModelReply(tool_calls=[{"id": f"c-{name}", "name": name, "arguments": arguments}])


def _turn(seeded, channel: str, who: str, text: str, *script: ModelReply):
    return agent.run_turn(seeded, channel, who, text, provider=ScriptedProvider(list(script)))


def _charts(reply) -> list[str]:
    labels = reply.attachment_labels or {}
    return [
        path
        for path in reply.attachments
        if str((labels.get(path) or {}).get("label") or "").endswith("size chart")
    ]


def _photos(reply) -> list[str]:
    charts = set(_charts(reply))
    return [path for path in reply.attachments if path not in charts]


def _ctx(seeded, text: str, channel: str = "whatsapp") -> ToolContext:
    return ToolContext(
        session=seeded, channel=channel, external_id="201021233010", history=[msg.user(text)]
    )


def _mirror_the_plain_tee(seeded, shopify, *, sleeve: str | None) -> Product:
    """The one product production has that the seed does not: made in the
    dashboard on 2026-09-22 and mirrored into wanas.db -- no `size_chart` row,
    its chart only a picture (`size_chart_image`), photos on Shopify's CDN,
    three colourways in S only, two of each."""
    product = Product(
        product_id=PLAIN,
        name="oversized plain t-shirt",
        category="T-Shirts",
        department="unisex",
        style=[],
        collection="Summer 2026",
        sleeve=sleeve,
        size_chart=None,
        size_chart_image=PLAIN_CHART,
        sizes=["S"],
        colors=["black", "navy", "white"],
        lengths=[],
        price=Decimal("300"),
        original_price=Decimal("300"),
        on_sale=False,
        images=list(PLAIN_PHOTOS.values()),
        color_images={colour: [path] for colour, path in PLAIN_PHOTOS.items()},
        description=(
            "Made from soft, comfortable fabric, this T-shirt features a classic crew "
            "neckline, dropped shoulders, wide sleeves, and an oversized fit for maximum "
            "comfort and a clean silhouette."
        ),
    )
    seeded.add(product)
    for colour in ("black", "navy", "white"):
        seeded.add(
            Variant(
                variant_id=f"{PLAIN}-s-{colour}",
                product_id=PLAIN,
                size="S",
                color=colour,
                price=Decimal("300"),
                original_price=Decimal("300"),
                on_sale=False,
                stock_qty=2,
            )
        )
    seeded.commit()
    shopify.seed_from(seeded)
    return product


# ==========================================================================
# 1. Chart or photos was decided by reading words that were not the
#    customer's, and by matching substrings of words that were
# ==========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "انا عايز تيشيرت اوفر سايز",  # whatsapp/201021233010 17:51:58
        "انا عايز تيشرت شبه دا ساده وافر سايز",  # instagram 21:53:19
        "عندك حاجة oversized؟",
        "عايز الرينجر boxy fit الأسود",
    ],
)
def test_a_style_name_is_not_a_sizing_question(seeded, text):
    assert not asked_about_sizing(_ctx(seeded, text)), text


def test_the_runtime_note_about_a_photo_is_not_the_customer_asking(seeded):
    """The note says «...السعر أو المقاسات أو التوفر» to the model. It was
    read as the customer asking about sizing *and* (from «صورة») asking for
    photos, so the chart went out beside the garment."""
    ctx = _ctx(seeded, PHOTO_NOTE, channel="instagram_dm")
    assert not asked_about_sizing(ctx)
    assert not showcase.asked_for_photos(ctx)


def test_a_quoted_bot_sentence_is_not_the_customer_asking_for_every_colour(seeded):
    from assistant.tools.base import asked_for_colors

    quoted = (
        '[the customer is replying to an earlier message -- you said: "بولو Knitted Polo — '
        'الألوان المتاحة: Burgundy و Navy و Olive و White"]\nعايز دا'
    )
    assert not asked_for_colors(_ctx(seeded, quoted))


def test_the_last_whatsapp_turn_17_52_keeps_its_product_photos(seeded):
    """«انا عايز تيشيرت اوفر سايز» is a product question. Production logged
    "showcase attached 3 photo(s)" and then "withheld 3 product photo(s): the
    customer asked about sizing, not for photos" -- the «سايز» of «اوفر
    سايز»."""
    reply = _turn(
        seeded,
        "whatsapp",
        "201021233010",
        "انا عايز تيشيرت اوفر سايز",
        _call("get_products", query="oversized تيشيرت"),
        ModelReply(
            text=(
                "عندنا أكتر من تيشيرت أوفر سايز، دول أبرزهم:\n"
                "• تيشيرت Cairokee T-shirt — السعر 600 جنيه، الألوان المتوفرة Brown و Black\n"
                "• تيشيرت Cairokee T-shirt 2 — السعر 600 جنيه، متوفر لون Black\n"
                "• تيشيرت Envy T-shirt — السعر 500 جنيه، متوفر لون Grey\n\n"
                "تحب أشوفلك واحد فيهم بالتفصيل؟"
            )
        ),
    )
    assert _photos(reply), "a product question gets product photos"
    assert _charts(reply) == []


def test_the_instagram_photo_turn_sends_photos_and_no_chart(seeded, shopify):
    """instagram 19:13 and 21:50/21:53: the customer sent a photo, the bot
    searched once and replied -- and the chart went out between the photos,
    three times."""
    _mirror_the_plain_tee(seeded, shopify, sleeve="half")
    reply = _turn(
        seeded,
        "instagram_dm",
        "1692370588503523",
        PHOTO_NOTE,
        _call("get_products", query="oversized plain t-shirt"),
        ModelReply(
            text=(
                "لو مقصدك تيشيرت oversized plain t-shirt، ده متوفر عندنا فعلاً — الألوان: "
                "Black و Navy و White، والسعر 300 جنيه. ده اللي كنت قاصده؟"
            )
        ),
    )
    assert PLAIN_CHART not in reply.attachments
    assert _photos(reply)


def test_a_chart_question_answered_from_memory_still_sends_the_chart(seeded):
    """whatsapp/201067177129 13:17: «طيب السايز شارت بتاع ringer boxy fit»,
    answered without a tool call («ده نفس السايز شارت اللي بعتهولك») -- and
    what went out was three Ringer photos and no chart."""
    who = "201067177129"
    history = [
        msg.user("طيب السايز شارت بتاع ringer boxy fit"),
        msg.assistant("", [{"id": "c1", "name": "get_size_chart", "arguments": {"product_id": "ringer-tee"}}]),
    ]
    ctx = ToolContext(session=seeded, channel="whatsapp", external_id=who, history=list(history))
    result = call_tool(ctx, "get_size_chart", {"product_id": "ringer-tee"})
    history.append(msg.tool_results([msg.tool_result("c1", "get_size_chart", result)]))
    history.append(msg.assistant("ده السايز شارت بتاع RINGER BOXY FIT TSHIRT."))
    from assistant import session as session_store

    session_store.save(seeded, "whatsapp", who, history)
    seeded.commit()

    reply = _turn(
        seeded,
        "whatsapp",
        who,
        "طيب السايز شارت بتاع ringer boxy fit",
        ModelReply(
            text=(
                "ده نفس السايز شارت اللي بعتهولك في الرسالة اللي فاتت — مقاسات "
                "RINGER BOXY FIT TSHIRT هي:\n\n"
                "• مقاس S — عرض 54 سم، طول 65 سم\n"
                "• مقاس M — عرض 56 سم، طول 67 سم\n"
                "• مقاس L — عرض 58 سم، طول 69 سم\n"
                "• مقاس XL — عرض 60 سم، طول 71 سم"
            )
        ),
    )
    assert _photos(reply) == []
    assert _charts(reply), "a size-chart question gets the chart"


# ==========================================================================
# 2. The catalog invented a sleeve length, and the prompt forbade doubting it
# ==========================================================================


def test_a_product_nobody_classified_is_not_described_as_long_sleeved(seeded, shopify):
    """The plain tee is short-sleeved (Shopify's own photos; its chart has a
    21 cm sleeve). Nothing ever recorded that, and `sleeves.for_category`
    answered "T-Shirts -> long" -- five replies in two channels said «كم
    طويل»."""
    _mirror_the_plain_tee(seeded, shopify, sleeve=None)
    variants = catalog.get_variants(seeded, PLAIN)
    assert variants.get("sleeve") is None
    found = catalog.get_products(seeded, query="oversized plain t-shirt")["products"]
    assert found and found[0].get("sleeve") is None
    assert PLAIN not in {
        p["product_id"] for p in catalog.get_products(seeded, sleeve="long")["products"]
    }


def test_the_boot_backfill_does_not_invent_a_sleeve(seeded, shopify):
    from domain.seed.products import backfill_sleeves

    _mirror_the_plain_tee(seeded, shopify, sleeve=None)
    backfill_sleeves(seeded)
    assert seeded.get(Product, PLAIN).sleeve is None


def test_the_sleeve_production_invented_is_corrected_at_boot(seeded, shopify):
    from domain.seed.products import correct_sleeves

    _mirror_the_plain_tee(seeded, shopify, sleeve="long")
    correct_sleeves(seeded)
    assert seeded.get(Product, PLAIN).sleeve == "half"


def test_a_sleeve_staff_chose_is_not_touched_by_the_correction(seeded, shopify):
    from domain.seed.products import correct_sleeves

    _mirror_the_plain_tee(seeded, shopify, sleeve="sleeveless")
    correct_sleeves(seeded)
    assert seeded.get(Product, PLAIN).sleeve == "sleeveless"


def test_the_new_product_form_does_not_preselect_long_sleeve():
    html = (Path(__file__).resolve().parent.parent / "dashboard" / "dashboard.html").read_text(
        encoding="utf-8"
    )
    start = html.index('id="nSleeve"')
    select = html[start : html.index("</select>", start)]
    assert "selected" not in select


def test_a_long_sleeve_claim_about_a_short_sleeved_tee_is_sent_back(seeded, shopify):
    """whatsapp/201021233010 17:52:56 -- «بس ده كم طويل مش نص كم» about a
    short-sleeved tee, to a customer who never mentioned sleeves."""
    _mirror_the_plain_tee(seeded, shopify, sleeve="half")
    fixed = "تيشيرت oversized plain t-shirt سادة ونص كم — السعر 300 جنيه، والمقاس المتاح S بس."
    reply = _turn(
        seeded,
        "whatsapp",
        "201021233010",
        "لا عايز حاجه ساده",
        _call("get_products", query="plain"),
        _call("get_variants", product_id=PLAIN),
        ModelReply(
            text=(
                "قريب من اللي انت طالبته: تيشيرت oversized plain t-shirt — السعر 300 جنيه، "
                "قماش ناعم وقطعة أوفر سايز فعلاً، بس ده كم طويل مش نص كم.\n\n"
                "الألوان المتوفرة: Black و Navy و White، والمقاس المتاح S بس."
            )
        ),
        ModelReply(text=fixed),
    )
    assert "كم طويل" not in reply.text
    assert reply.text == fixed


def test_a_sleeve_claim_about_an_unclassified_product_is_sent_back(seeded, shopify):
    _mirror_the_plain_tee(seeded, shopify, sleeve=None)
    fixed = "تيشيرت oversized plain t-shirt — السعر 300 جنيه، والمقاس المتاح S بس."
    reply = _turn(
        seeded,
        "instagram_dm",
        "1692370588503523",
        PHOTO_NOTE,
        _call("get_products", query="oversized plain t-shirt"),
        ModelReply(
            text=(
                "لو تحب، ده تيشيرت oversized plain t-shirt — كم طويل، متوفر بألوان Black و "
                "Navy و White بس مقاس S، والسعر 300 جنيه."
            )
        ),
        ModelReply(text=fixed),
    )
    assert reply.text == fixed


def test_saying_the_sleeve_is_not_recorded_is_allowed_when_it_is_not(seeded, shopify):
    """The old rule failed *any* «مش متسجّل» beside a sleeve word, which is
    what made an invented value the only answer the bot was allowed."""
    from assistant import reply_rules

    _mirror_the_plain_tee(seeded, shopify, sleeve=None)
    history = [
        msg.user("ده نص كم؟"),
        msg.assistant("", [{"id": "v", "name": "get_variants", "arguments": {"product_id": PLAIN}}]),
        msg.tool_results([msg.tool_result("v", "get_variants", catalog.get_variants(seeded, PLAIN))]),
    ]
    text = "طول الكم مش متسجّل عندي للتيشيرت ده، بس دي صورته وممكن تشوف منها."
    assert reply_rules.violation(text, customer="ده نص كم؟", previous="", results=[], history=history) == ""


# ==========================================================================
# 3. The prompt handed the model facts the shop never published
# ==========================================================================


def test_the_prompt_does_not_teach_a_delivery_range_the_shop_never_published():
    """The layout example «من 2 لـ 4 أيام» was quoted as the delivery time
    (whatsapp 22 Sep 15:00, instagram 25 Sep 21:54); the published promise is
    «بياخد لغاية 4 أيام»."""
    from assistant.prompt import SYSTEM_PROMPT

    assert "2 لـ 4" not in SYSTEM_PROMPT


def test_the_prompt_no_longer_forbids_an_honest_unknown_sleeve():
    from assistant.prompt import SYSTEM_PROMPT

    assert "مفيش «مش متسجّل»" not in SYSTEM_PROMPT


def test_a_delivery_minimum_nobody_published_is_sent_back(seeded):
    from assistant import reply_facts
    from domain.models import ShippingRate

    for rate in seeded.query(ShippingRate).all():
        rate.fee = 110  # production's flat rate
    seeded.commit()

    reply = (
        "الشحن 110 جنيه لكل محافظات مصر، والتوصيل بياخد من 2 لـ 4 أيام.\n\n"
        "محافظتك إيه عشان أحسبلك الإجمالي؟"
    )
    history = [msg.user("اه تمام بس عايز اعرف سعر الشحن ومدته قد ايه")]
    made_up = reply_facts.ungrounded(reply, history, reply_facts.shop_constants(seeded))
    assert made_up and any("2" in item for item in made_up)
    published = "الشحن 110 جنيه لكل محافظات مصر، والتوصيل بياخد لغاية 4 أيام."
    assert reply_facts.ungrounded(published, history, reply_facts.shop_constants(seeded)) == []


# ==========================================================================
# 4. Numbers the model made up that no guard read: stock counts
# ==========================================================================


def test_a_stock_count_no_tool_returned_is_sent_back(seeded, shopify):
    """instagram 19:13:49 -- «باقي حاجة واحدة لكل لون تقريباً» when
    get_variants said two of each."""
    from assistant import reply_facts

    _mirror_the_plain_tee(seeded, shopify, sleeve="half")
    history = [
        msg.user(PHOTO_NOTE),
        msg.assistant("", [{"id": "v", "name": "get_variants", "arguments": {"product_id": PLAIN}}]),
        msg.tool_results([msg.tool_result("v", "get_variants", catalog.get_variants(seeded, PLAIN))]),
        msg.user("دا موجود؟"),
    ]
    text = "أيوه موجود، بس بالكمية قليلة — باقي حاجة واحدة لكل لون تقريباً. تحب أحجزلك لون ولا مقاس معين؟"
    assert reply_facts.ungrounded(text, history, reply_facts.shop_constants(seeded))
    true = "أيوه موجود، بس الكمية قليلة — باقي قطعتين من كل لون."
    assert reply_facts.ungrounded(true, history, reply_facts.shop_constants(seeded)) == []


# ==========================================================================
# 5. The words and the pictures disagreed: an offer to show photos that are
#    already attached
# ==========================================================================


def test_offering_photos_the_reply_is_already_carrying_is_sent_back(seeded):
    """instagram 21:50:47 -- a list of three tees, two photos attached, and
    «تحب أوريك صور واحد فيهم؟»."""
    fixed = (
        "أيوه، عندنا تيشيرتات قريبة:\n"
        "• تيشيرت Envy T-shirt — السعر 500 جنيه\n"
        "• تيشيرت Cairokee T-shirt — السعر 600 جنيه\n\n"
        "عجبك أنهي واحد؟"
    )
    reply = _turn(
        seeded,
        "instagram_dm",
        "1692370588503523",
        "هو في تيشرت شبه دا عندكو",
        _call("get_products", category="T-Shirts"),
        ModelReply(
            text=(
                "أيوه، عندنا تيشيرتات شبهه. الأقرب:\n"
                "• تيشيرت Envy T-shirt — السعر 500 جنيه\n"
                "• تيشيرت Cairokee T-shirt — السعر 600 جنيه\n\n"
                "تحب أوريك صور واحد فيهم؟"
            )
        ),
        ModelReply(text=fixed),
    )
    assert _photos(reply)
    assert reply.text == fixed


def test_an_offer_the_model_keeps_writing_is_removed_rather_than_sent(seeded):
    offer = "\n".join(
        [
            "أيوه، عندنا تيشيرتات شبهه. الأقرب:",
            "• تيشيرت Envy T-shirt — السعر 500 جنيه",
            "• تيشيرت Cairokee T-shirt — السعر 600 جنيه",
            "",
            "تحب أوريك صور واحد فيهم؟",
        ]
    )
    reply = _turn(
        seeded,
        "instagram_dm",
        "1692370588503523",
        "هو في تيشرت شبه دا عندكو",
        _call("get_products", category="T-Shirts"),
        *[ModelReply(text=offer) for _ in range(4)],
    )
    assert _photos(reply)
    assert "أوريك" not in reply.text
    assert reply.text.startswith("أيوه، عندنا تيشيرتات شبهه.")


def test_what_is_not_an_offer_of_photos():
    from assistant import photo_claims

    for text in ("تحب أشوفلك واحد فيهم بالتفصيل؟", "تحب تشوف جدول مقاساته ولا نضيفه؟"):
        assert photo_claims.offers_photos(text) == "", text
    for text in ("تحب تشوفه ولا نكمل؟", "تحب تشوف صوره؟", "تحب أبعتلك صورهم؟"):
        assert photo_claims.offers_photos(text), text


# ==========================================================================
# 6. An answer the customer never gave: link_client in the same breath as
#    the question (whatsapp/201021233010, 22 Sep 15:08)
# ==========================================================================


def test_is_this_you_cannot_be_answered_by_the_bot_itself(seeded):
    who = "201021233010"
    other = Client(full_name="محمد ابراهيم", phone="01021233010", address="كفر المصيلحه - الساقيه", governorate="Monufia")
    seeded.add(other)
    seeded.flush()
    identity = identities.get_or_create(seeded, "whatsapp", who)
    identities.note_pending_link(seeded, identity, other, "phone")
    seeded.commit()

    history = [
        msg.user("تمام"),
        msg.assistant("", [{"id": "p", "name": "get_my_profile", "arguments": {}}]),
    ]
    ctx = ToolContext(session=seeded, channel="whatsapp", external_id=who, history=history)
    profile = call_tool(ctx, "get_my_profile", {})
    assert profile["pending_link"]
    history.append(msg.tool_results([msg.tool_result("p", "get_my_profile", profile)]))
    history.append(
        msg.assistant(
            "عندنا سجل تاني بنفس رقم تليفون حضرتك — ده انت؟",
            [{"id": "l", "name": "link_client", "arguments": {"confirmed": True}}],
        )
    )

    result = call_tool(ctx, "link_client", {"confirmed": True})
    assert result.get("error") == "not_asked_yet"
    assert "address" not in result
    assert identities.get(seeded, "whatsapp", who).client_id is None

    # ...and once the customer has actually answered, it links.
    history.append(msg.tool_results([msg.tool_result("l", "link_client", result)]))
    history.append(msg.assistant("عندنا سجل تاني بنفس رقم تليفون حضرتك — ده انت؟"))
    history.append(msg.user("اه ده انا"))
    assert call_tool(ctx, "link_client", {"confirmed": True}).get("linked") is True


# ==========================================================================
# 7. What left, on the record: which pictures a reply sent
# ==========================================================================


def test_every_picture_sent_is_logged_as_chart_or_photo():
    from types import SimpleNamespace

    from assistant import showcase

    outcomes = [
        SimpleNamespace(delivered=True, image_path=None),  # the text
        SimpleNamespace(delivered=True, image_path="data/size-charts/wns-boxy-tee.png"),
        SimpleNamespace(delivered=False, image_path=CDN + "DSC02008.jpg"),
    ]
    labels = {
        "data/size-charts/wns-boxy-tee.png": {"label": "Boxy WNS Tee size chart"},
        CDN + "DSC02008.jpg": {"label": "Boxy WNS Tee (Black)"},
    }
    assert showcase.sent_pictures(outcomes, labels) == (
        "chart[Boxy WNS Tee size chart]=ok, photo[Boxy WNS Tee (Black)]=REFUSED"
    )


# ==========================================================================
# 8. The Instagram conversation that ended in #1042, reviewed again
#    (names, phone and address below are placeholders)
# ==========================================================================

IG_WHO = "ig-customer-placeholder"
PHONE = "01000000017"


def _monufia_at_110(seeded) -> None:
    from domain.models import ShippingRate

    seeded.get(ShippingRate, "Monufia").fee = 110
    seeded.commit()


def _checkout_so_far(seeded, shopify):
    """21:53:57 add_to_cart, then 21:54-21:57 exactly as it ran: fee and total
    given once («الإجمالي 410 جنيه… تحب نكمّل؟»), then name, address and phone
    collected one by one -- and no summary after them."""
    _mirror_the_plain_tee(seeded, shopify, sleeve="half")
    _monufia_at_110(seeded)
    history = [msg.user("طب أنا عايز الأسود مقاس الـ Small عادي.")]
    ctx = ToolContext(session=seeded, channel="instagram_dm", external_id=IG_WHO, history=history)
    call_tool(ctx, "add_to_cart", {"variant_id": f"{PLAIN}-s-black"})
    history += [
        msg.assistant("تمام، ضفنالك تيشيرت oversized plain t-shirt — مقاس S، لون Black، بـ 300 جنيه.\n\nتحب نكمّل بيانات التوصيل؟"),
        msg.user("اه تمام بس عايز اعرف سعر الشحن ومدته قد ايه"),
        msg.assistant("الشحن 110 جنيه لكل محافظات مصر، والتوصيل بياخد لغاية 4 أيام.\n\nمحافظتك إيه عشان أحسبلك الإجمالي؟"),
        msg.user("المنوفية"),
    ]
    fee = call_tool(ctx, "get_shipping_fee", {"governorate": "المنوفية"})
    assert fee["checkout"]["total"] == 410
    history += [
        msg.tool_results([msg.tool_result("f", "get_shipping_fee", fee)]),
        msg.assistant("الشحن للمنوفية 110 جنيه، فالإجمالي 410 جنيه كاش عند الاستلام.\n\nتحب نكمّل؟ محتاج منك اسمك والعنوان بالتفصيل ورقم تليفون."),
        msg.user("عميل تجريبي"),
        msg.assistant("تمام. عايز العنوان بالتفصيل — الشارع والعمارة والشقة وأي علامة مميزة."),
        msg.user("المنوفيه، مدينة تجريبية، قرية تجريبية"),
        msg.assistant("تمام. عايز تفاصيل أكتر من كده عشان المندوب — اسم الشارع أو العمارة ورقم الشقة، وأي علامة مميزة قريبة."),
        msg.user(f"قوله عند النادي وهو هيعرف\n{PHONE}"),
    ]
    return ctx


ORDER_ARGS = {
    "address": "مدينة تجريبية، قرية تجريبية، عند النادي",
    "contact_phone": PHONE,
    "customer_name": "عميل تجريبي",
    "governorate": "المنوفية",
}


def test_no_order_is_placed_before_the_customer_says_yes_to_a_summary(seeded, shopify):
    """21:57:20 -- confirm_order ran on «قوله عند النادي وهو هيعرف» plus a
    phone number. No summary of name, address and total had been shown since
    the details were given, and nobody said yes: order #1042 was real."""
    from domain.models import Order

    ctx = _checkout_so_far(seeded, shopify)
    refused = call_tool(ctx, "confirm_order", dict(ORDER_ARGS))
    assert refused.get("error") == "not_confirmed_by_customer"
    assert seeded.query(Order).count() == 0

    ctx.history += [
        msg.tool_results([msg.tool_result("o", "confirm_order", refused)]),
        msg.assistant(
            "ملخص الأوردر:\n• تيشيرت oversized plain t-shirt — مقاس S، لون Black، السعر 300 جنيه\n"
            "• الشحن للمنوفية — 110 جنيه\n• الإجمالي — 410 جنيه كاش عند الاستلام\n\nأأكد الأوردر؟"
        ),
        msg.user("اه"),
    ]
    placed = call_tool(ctx, "confirm_order", dict(ORDER_ARGS))
    assert placed.get("order_id"), placed


def test_a_summary_followed_by_new_details_is_not_a_yes(seeded, shopify):
    """The total was shown at 21:55:28 and the customer answered with their
    name -- not with a yes to that summary, and not after the address."""
    ctx = _checkout_so_far(seeded, shopify)
    ctx.history[-1] = msg.user("اه تمام " + PHONE)  # agreeing -- to the address question
    assert call_tool(ctx, "confirm_order", dict(ORDER_ARGS)).get("error") == "not_confirmed_by_customer"


def test_offering_to_hold_stock_the_shop_cannot_hold_is_corrected():
    """19:13:49 «تحب أحجزلك لون ولا مقاس معين؟» -- there is no reservation;
    the only thing the bot can do is add it to the cart."""
    from assistant import reply_rules

    fixed, fixes = reply_rules.fix_arabic("أيوه موجود. تحب أحجزلك لون ولا مقاس معين؟")
    assert "أحجزلك" not in fixed and "أضيفلك" in fixed and fixes
