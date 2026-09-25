"""A reply about the merchandise shows the merchandise, without being asked.

    customer: «عندكم هوديز؟»
    log:      tool get_products({'query': 'hoodie'})
    bot:      «عندنا WANAS Hoodie و WANAS Zip-Hoodie ...»        -> no photo
    customer: «طب ابعتلي صورة»                                 -> now one

The photographs used to depend on the model deciding to call `get_variants`,
and on a search landing on exactly one product. A reply built from a search
with several hits -- the ordinary way a conversation about clothes starts --
carried none at all. `assistant/showcase.py` decides from the finished reply
instead: every catalog product it names gets its photo, and one product shown
alone for the first time gets its other in-stock colourways too.
"""

from __future__ import annotations

from assistant import agent, session as session_store, showcase
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider

CHANNEL = "whatsapp"
WHO = "201000000777"


def calls(*pairs) -> ModelReply:
    return ModelReply(
        tool_calls=[
            {"id": f"c{index}", "name": name, "arguments": arguments}
            for index, (name, arguments) in enumerate(pairs)
        ]
    )


def turn(session, message: str, *replies: ModelReply):
    provider = ScriptedProvider(list(replies))
    return agent.run_turn(session, CHANNEL, WHO, message, provider=provider), provider


def shown(reply) -> dict[str, list[str]]:
    """Product -> the photographs of it this reply carries (charts excluded)."""
    out: dict[str, list[str]] = {}
    for path in reply.attachments:
        label = reply.attachment_labels.get(path) or {}
        if str(label.get("label", "")).endswith("size chart"):
            continue
        out.setdefault(label.get("product_id"), []).append(path)
    return out


def colour_of(reply, path: str) -> str | None:
    return (reply.attachment_labels.get(path) or {}).get("color")


# --- the complaint ------------------------------------------------------------


def test_a_browse_answer_shows_each_product_it_names(seeded):
    reply, _ = turn(
        seeded,
        "عندكم هوديز؟",
        calls(("get_products", {"query": "hoodie"})),
        ModelReply(text="أيوه، عندنا WANAS Hoodie و WANAS Zip-Hoodie. تحب تشوف أنهي فيهم أكتر؟"),
    )
    photos = shown(reply)
    assert set(photos) == {"wanas-hoodie", "wanas-zip-hoodie"}
    assert all(len(paths) == 1 for paths in photos.values()), "a list shows one photo each"
    assert reply.error is None


def test_one_product_shown_alone_the_first_time_brings_its_colourways(seeded):
    reply, _ = turn(
        seeded,
        "عايز الرينجر",
        calls(("get_variants", {"product_id": "ringer-tee", "color": "Navy"})),
        ModelReply(text="ده Ringer Tee، متاح بأربع ألوان والمقاسات من S لـ XL."),
    )
    photos = shown(reply)["ringer-tee"]
    assert len(photos) == showcase.FIRST_SHOWING_PHOTOS
    assert colour_of(reply, photos[0]) == "Navy", "the colour asked for leads"
    assert len({colour_of(reply, p) for p in photos}) == len(photos), "one per colourway"


def test_only_colourways_that_can_be_bought_are_offered(seeded):
    """The WANAS Hoodie's grey is sold out: showing it is inviting an order
    the shop cannot take."""
    reply, _ = turn(
        seeded,
        "الهودي",
        calls(("get_variants", {"product_id": "wanas-hoodie"})),
        ModelReply(text="ده WANAS Hoodie، متاح Black و Olive."),
    )
    colours = {colour_of(reply, p) for p in shown(reply)["wanas-hoodie"]}
    assert colours == {"Black", "Olive"}


def test_a_colour_named_in_arabic_beside_the_product_leads(seeded):
    reply, _ = turn(
        seeded,
        "عندكم هوديز؟",
        calls(("get_products", {"query": "hoodie"})),
        ModelReply(text="عندنا WANAS Hoodie الزيتي متاح بكل المقاسات."),
    )
    photos = shown(reply)["wanas-hoodie"]
    assert colour_of(reply, photos[0]) == "Olive"


# --- what it must not do ------------------------------------------------------


def test_a_product_nothing_of_which_can_be_bought_is_not_shown(seeded):
    reply, _ = turn(
        seeded,
        "عندكم هوديز؟",
        calls(("get_products", {"query": "hoodie"})),
        ModelReply(text="الـ Cairokee Hoodie خلص للأسف، بس عندنا WANAS Hoodie."),
    )
    assert "cairokee-hoodie" not in shown(reply)
    assert "wanas-hoodie" in shown(reply)


def test_a_name_no_tool_returned_gets_no_photo(seeded):
    """A product the model made up has no id to look up and no picture -- and
    the claim check still catches a sentence promising one."""
    reply, provider = turn(
        seeded,
        "عندكم جينز؟",
        calls(("get_products", {"query": "hoodie"})),
        ModelReply(text="دي صورة WANAS Denim Jacket 👆"),
        ModelReply(text="للأسف مفيش جينز عندنا."),
    )
    assert reply.attachments == []
    assert len(provider.calls) == 3, "the unbacked photo claim was still retried"


def test_an_order_turn_shows_no_merchandise(seeded):
    session_store.save(
        seeded,
        CHANNEL,
        WHO,
        [
            {"role": "user", "content": "عندكم هوديز؟"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "p1", "name": "get_products", "arguments": {"query": "hoodie"}}],
            },
            {
                "role": "tool_results",
                "results": [
                    {
                        "id": "p1",
                        "name": "get_products",
                        "content": {
                            "products": [{"product_id": "wanas-hoodie", "name": "WANAS Hoodie"}],
                            "count": 1,
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "عندنا WANAS Hoodie."},
        ],
    )
    reply, _ = turn(
        seeded,
        "أوردري فين؟",
        calls(("get_my_orders", {})),
        ModelReply(text="أوردرك فيه WANAS Hoodie ولسه ماتشحنش."),
    )
    assert reply.attachments == []


def test_a_product_already_shown_is_not_shown_again(seeded):
    first, _ = turn(
        seeded,
        "عايز الرينجر",
        calls(("get_variants", {"product_id": "ringer-tee"})),
        ModelReply(text="ده Ringer Tee."),
    )
    assert first.attachments

    again, _ = turn(seeded, "بكام؟", ModelReply(text="الـ Ringer Tee بـ 450 جنيه."))
    assert again.attachments == []


def test_a_new_colour_of_a_product_already_shown_is_shown(seeded):
    """A product seen in black and asked about in olive is a new question --
    the same rule the tool layer keeps."""
    turn(
        seeded,
        "الهودي الأسود",
        calls(("get_variants", {"product_id": "wanas-hoodie", "color": "Black"})),
        ModelReply(text="ده WANAS Hoodie الأسود."),
    )
    # The first showing sent every in-stock colourway; forget the olive one
    # to ask the question the rule is about.
    history = session_store.load(seeded, CHANNEL, WHO)
    for message in history:
        if message.get("role") == "assistant" and message.get("attachments"):
            message["attachments"] = message["attachments"][:1]
    session_store.save(seeded, CHANNEL, WHO, history)

    reply, _ = turn(seeded, "والزيتي؟", ModelReply(text="WANAS Hoodie الزيتي متاح كمان."))
    photos = shown(reply)["wanas-hoodie"]
    assert [colour_of(reply, p) for p in photos] == ["Olive"]


def test_two_products_asked_for_by_both_get_one_photo_each(seeded):
    """«الاتنين» is still two photographs, not every colourway of both."""
    reply, _ = turn(
        seeded,
        "الاتنين",
        calls(
            ("get_variants", {"product_id": "wanas-sweatpant"}),
            ("get_variants", {"product_id": "lightweight-sweatpant"}),
        ),
        ModelReply(text="دي صورة WANAS Sweatpant ودي صورة Lightweight Sweatpant 👆"),
    )
    photos = shown(reply)
    assert {k: len(v) for k, v in photos.items()} == {
        "wanas-sweatpant": 1,
        "lightweight-sweatpant": 1,
    }


def test_a_long_list_shows_the_first_few(seeded):
    reply, _ = turn(
        seeded,
        "وريني كل حاجة",
        calls(("get_products", {})),
        ModelReply(
            text="عندنا WANAS Hoodie و WANAS Polo و Knitted Polo و Worker Jacket "
            "و Ringer Tee و Lightweight Sweatpant."
        ),
    )
    photos = shown(reply)
    assert len(photos) == showcase.MAX_SHOWCASE_PRODUCTS
    assert sum(len(v) for v in photos.values()) <= showcase.MAX_SHOWCASE_PHOTOS
    assert list(photos)[:2] == ["wanas-hoodie", "wanas-polo"], "in the order the reply names them"


def test_a_retried_reply_does_not_keep_the_photos_of_the_sentence_it_replaced(seeded):
    reply, _ = turn(
        seeded,
        "الصورة مش واصلة",
        calls(("get_products", {"query": "polo"})),
        ModelReply(text="دي صورة WANAS Polo 👆 ولو مش ظاهرة جرب اقفل الواتس وافتحه."),
        ModelReply(text="معلش المشكلة من عندنا، ده Knitted Polo 👆"),
    )
    assert set(shown(reply)) == {"knitted-polo"}


# --- naming ---------------------------------------------------------------------


KNOWN = {
    "Cairokee T-shirt": "cairokee-tee",
    "Cairokee T-shirt 2": "cairokee-tee-2",
    "Ringer Tee": "ringer-tee",
    "WANAS Hoodie": "wanas-hoodie",
    "WANAS Zip-Hoodie": "wanas-zip-hoodie",
}


def ids(text: str) -> list[str]:
    return [hit[0] for hit in showcase.named_products(text, KNOWN)]


def test_a_longer_name_is_not_also_read_as_the_shorter_one():
    assert ids("عندنا Cairokee T-shirt 2 بالأسود") == ["cairokee-tee-2"]
    assert ids("Cairokee T-shirt 2 و Cairokee T-shirt") == ["cairokee-tee-2", "cairokee-tee"]
    assert ids("WANAS Zip-Hoodie") == ["wanas-zip-hoodie"]


def test_a_distinctive_word_is_enough_and_a_shared_one_is_not():
    assert ids("الـ Ringer متاح") == ["ringer-tee"]
    assert ids("عندنا حاجات Cairokee كتير") == [], "three products share that word"
    assert ids("WANAS") == []


def test_names_match_whatever_the_case():
    assert ids("wanas hoodie") == ["wanas-hoodie"]
    assert ids("RINGER TEE") == ["ringer-tee"]


def test_colours_are_read_in_either_language():
    colours = ["Black", "Olive", "Camel Brown"]
    assert showcase.colour_named("الزيتي حلو", colours) == "Olive"
    assert showcase.colour_named("in Camel Brown", colours) == "Camel Brown"
    assert showcase.colour_named("الأسود", colours) == "Black"
    assert showcase.colour_named("متاح بكل المقاسات", colours) is None
