"""A reply may not say more about photographs than it is actually sending.

One production conversation, four separate failures, and each test below is
one of them:

    bot:      lists two sweatpants, «تحب تشوف صور واحد فيهم؟»
    customer: «الاتنين»
    bot:      «دي صورتهم الاتنين 👆»       -> one photo went, one was refused
    customer: «انت باعت صوره واحد مش اتنين ياريس»
    bot:      «معلش ياريس، دي صورة Lightweight الأسود 👆 ...»   -> no photo
    customer: «انت مش باعت صور اصلا انا عايز صوره المنتجين»
    bot:      «... ممكن تكون مشكلة في النت أو التطبيق — جرب اقفل الواتس»
              -> then every colourway of both products

The words are composed by the model and the pictures are attached by the tool
layer, and nothing joined the two back together before the reply left. The
platform's refusal, meanwhile, arrived *after* the reply was stored, so from
inside the next turn a photo that failed looked exactly like one that landed --
which is how the shop came to argue with a customer who was simply right.
"""

from __future__ import annotations

from assistant import agent, photo_claims, session as session_store
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.tools.base import ToolContext, call_tool
from domain.services.notifications import OutboundMessage

CHANNEL = "whatsapp"
WHO = "201000000001"

CHART_DIR = "data/size-charts/"


def reply_with(*calls) -> ModelReply:
    return ModelReply(
        tool_calls=[
            {"id": f"c{index}", "name": name, "arguments": arguments}
            for index, (name, arguments) in enumerate(calls)
        ]
    )


def photos(ctx) -> list[str]:
    return [p for p in ctx.attachments if not p.startswith(CHART_DIR)]


# --- 1. the sentence and the attachments have to agree ---------------------


def test_claiming_both_while_sending_one_is_caught():
    """The first message in the conversation above, exactly."""
    labels = {"a.jpg": {"label": "Lightweight Sweatpant (Black)", "name": "Lightweight Sweatpant"}}
    why = photo_claims.unbacked_claim(
        "دي صورتهم الاتنين 👆",
        attachments=["a.jpg"],
        labels=labels,
    )
    assert "claims more than one photo" in why


def test_claiming_a_product_no_photo_was_attached_for_is_caught():
    """The second message: it names two products beside the word «صورة» and
    carries a picture of one of them."""
    history = [
        {
            "role": "tool_results",
            "results": [
                {
                    "id": "c0",
                    "name": "get_products",
                    "content": {
                        "products": [
                            {"product_id": "lightweight-sweatpant", "name": "Lightweight Sweatpant"},
                            {"product_id": "wanas-sweatpant", "name": "WANAS Sweatpant"},
                        ]
                    },
                }
            ],
        }
    ]
    why = photo_claims.unbacked_claim(
        "دي صورة Lightweight الأسود 👆 ودي اللي وصلت قبل كده كانت WANAS Sweatpant",
        attachments=["a.jpg"],
        labels={"a.jpg": {"label": "Lightweight Sweatpant (Black)", "name": "Lightweight Sweatpant"}},
        history=history,
    )
    assert "WANAS Sweatpant" in why


def test_one_photo_described_as_one_photo_is_fine():
    assert (
        photo_claims.unbacked_claim(
            "دي صورة تيشيرت Ringer Tee 👆",
            attachments=["a.jpg"],
            labels={"a.jpg": {"label": "Ringer Tee (Navy)", "name": "Ringer Tee"}},
        )
        == ""
    )


def test_two_photos_described_as_two_is_fine():
    labels = {
        "a.jpg": {"label": "Ringer Tee (Navy)", "name": "Ringer Tee"},
        "b.jpg": {"label": "Envy T-shirt (Black)", "name": "Envy T-shirt"},
    }
    assert (
        photo_claims.unbacked_claim(
            "دي صور الاتنين 👆", attachments=["a.jpg", "b.jpg"], labels=labels
        )
        == ""
    )


def test_a_size_chart_is_not_a_photo_of_the_garment():
    """A chart is a picture of a table. A reply that sends one and says it is
    sending the product's photo has still not shown the customer the garment."""
    labels = {"chart.png": {"label": "Ringer Tee size chart", "name": "Ringer Tee"}}
    why = photo_claims.unbacked_claim(
        "دي صورة تيشيرت Ringer Tee 👆", attachments=["chart.png"], labels=labels
    )
    assert "nothing is attached" in why


def test_a_customers_own_photo_exempts_the_whole_check():
    assert (
        photo_claims.unbacked_claim(
            "وصلتني الصورة، وأقرب حاجة عندنا ليها الهودي الأسود",
            attachments=[],
            customer_sent_a_photo=True,
        )
        == ""
    )


def test_the_agent_sends_what_it_attached_rather_than_what_it_claimed(seeded):
    """Two nudges, and if the model will not write a sentence that matches the
    reply, the reply says what is true of itself -- keeping the photograph
    that really did go rather than throwing it away for a question."""
    provider = ScriptedProvider(
        [
            reply_with(("get_variants", {"product_id": "wanas-hoodie"})),
            ModelReply(text="دي صورتهم الاتنين 👆"),
            ModelReply(text="دي الصور بتاعت الاتنين"),
            ModelReply(text="اتفضل، دي صورهم الاتنين"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ابعتلي صورة الاتنين", provider=provider)
    assert reply.text == agent.PARTIAL_IMAGE_FALLBACK
    assert reply.error == "image_promise"
    assert reply.attachments, "the photo that really was attached must still go"


# --- 2. a refused photo is not a photo the customer has seen ---------------


def _reply_with_photo(session, path="data/images/wanas-black-hoodie/01.jpg"):
    session_store.save(
        session,
        CHANNEL,
        WHO,
        [
            {"role": "user", "content": "ابعتلي صورة"},
            {"role": "assistant", "content": "دي صورته 👆", "attachments": [path]},
        ],
    )
    return path


def test_a_refused_photo_is_taken_back_out_of_already_sent(seeded):
    path = _reply_with_photo(seeded)
    session_store.record_undelivered_attachments(
        seeded, CHANNEL, WHO, {path: "WANAS Hoodie (Black)"}
    )

    stored = session_store.transcript(seeded, CHANNEL, WHO)[-1]
    assert stored["attachments"] == []
    assert stored["undelivered_attachments"] == {path: "WANAS Hoodie (Black)"}
    assert photo_claims.undelivered(stored and [stored]) == {path: "WANAS Hoodie (Black)"}


def test_the_turn_is_told_about_it_and_may_send_it_again(seeded):
    """Both halves matter. The model has to know the customer is right, and
    the image policy has to stop treating the picture as already shown --
    otherwise the one photo they are asking for is the one it will never
    send."""
    path = _reply_with_photo(seeded)
    session_store.record_undelivered_attachments(seeded, CHANNEL, WHO, {path: "WANAS Hoodie (Black)"})

    provider = ScriptedProvider(
        [
            reply_with(("get_variants", {"product_id": "wanas-hoodie", "color": "Black"})),
            ModelReply(text="معلش، الصورة اتأخرت من عندنا. دي صورته تاني 👆"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الصورة مش وصلت", provider=provider)
    assert path in reply.attachments, "the refused photo has to be sendable again"
    sent_prompt = provider.calls[0][0] if provider.calls else ""
    assert "المنصة رفضتها" in sent_prompt
    assert "WANAS Hoodie (Black)" in sent_prompt


def test_the_adapter_pairs_a_failed_send_with_what_it_was_of():
    outcomes = [
        OutboundMessage(to=WHO, text="دي صورهم", message_ids=["wamid.1"]),
        OutboundMessage(to=WHO, text="", kind="image", image_path="a.jpg", message_ids=["wamid.2"]),
        OutboundMessage(
            to=WHO, text="", kind="image", image_path="b.jpg", delivered=False, error="rejected"
        ),
    ]
    labels = {
        "a.jpg": {"label": "Ringer Tee (Navy)"},
        "b.jpg": {"label": "Envy T-shirt (Black)"},
    }
    assert session_store.undelivered_photos(outcomes, labels) == {"b.jpg": "Envy T-shirt (Black)"}


# --- 3. our failure is never the customer's to debug -----------------------


def test_telling_the_customer_to_restart_whatsapp_is_caught():
    assert photo_claims.blames_the_customer(
        "لو الصور لسه مش بتوصلك ممكن تكون مشكلة في النت أو التطبيق — جرب اقفل الواتس وافتحه تاني"
    )


def test_saying_the_problem_is_ours_is_not():
    assert (
        photo_claims.blames_the_customer("معلش، الصورة مش راضية تتبعت من عندنا إحنا. بجربها تاني")
        == ""
    )


def test_the_agent_replaces_a_blaming_reply_with_an_honest_one(seeded):
    blame = "الصور اتبعتت خلاص. جرب اقفل الواتس وافتحه تاني."
    provider = ScriptedProvider([ModelReply(text=blame)] * 3)
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الصور مش وصلت", provider=provider)
    assert reply.text == agent.BLAME_FALLBACK
    assert reply.error == "blamed_the_customer"
    assert "من ناحيتنا إحنا" in reply.text


# --- 4. "both" is two photographs, not two galleries -----------------------


def test_both_products_get_one_photo_each(seeded):
    """The budget follows the *turn*, not the tool call. Each `get_variants`
    knows only about its own product's pictures, so counting across the reply
    is the only place this can be a guarantee."""
    ctx = ToolContext(
        session=seeded,
        channel=CHANNEL,
        external_id=WHO,
        history=[{"role": "user", "content": "الاتنين"}],
    )
    call_tool(ctx, "get_variants", {"product_id": "wanas-hoodie"})
    call_tool(ctx, "get_variants", {"product_id": "ringer-tee"})
    assert len(photos(ctx)) == 2
    assert ctx.photos_of("wanas-hoodie") == 1
    assert ctx.photos_of("ringer-tee") == 1


def test_more_images_without_a_colour_request_is_not_a_gallery(seeded):
    """`more_images` is one model-set flag covering two different asks. The
    colour gallery is the expensive half and now needs the customer's own
    words behind it -- «الاتنين» is not one of them."""
    ctx = ToolContext(
        session=seeded,
        channel=CHANNEL,
        external_id=WHO,
        history=[{"role": "user", "content": "الاتنين"}],
    )
    call_tool(ctx, "get_variants", {"product_id": "ringer-tee", "more_images": True})
    assert len(photos(ctx)) <= 2, "four colourways went out for a two-word message"


def test_asking_for_the_colours_still_gets_them(seeded):
    ctx = ToolContext(
        session=seeded,
        channel=CHANNEL,
        external_id=WHO,
        history=[{"role": "user", "content": "ابعتلي صور كل الألوان"}],
    )
    payload = call_tool(ctx, "get_variants", {"product_id": "ringer-tee", "more_images": True})
    assert len(photos(ctx)) == len(payload["color_images"]) == 4


def test_the_size_chart_does_not_eat_the_products_one_photo(seeded):
    """A chart is not a garment photo and must not consume the budget: the
    reply that arrived with the chart alone would have shown the customer a
    measurements table instead of the thing they asked to see."""
    ctx = ToolContext(
        session=seeded,
        channel=CHANNEL,
        external_id=WHO,
        history=[{"role": "user", "content": "المقاسات إيه؟"}],
    )
    call_tool(ctx, "get_variants", {"product_id": "wanas-hoodie"})
    assert len(photos(ctx)) == 1
    assert [p for p in ctx.attachments if p.startswith(CHART_DIR)]
