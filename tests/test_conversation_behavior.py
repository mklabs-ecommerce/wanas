"""Regressions from real harness testing.

Two kinds of coverage live here, and the split is deliberate.

The tests in this file are deterministic: they pin the *machinery* that makes
good conversation possible (history reaching the model, a compound request
resolving in one pass, images actually being attached) and the *instructions*
that make it likely (the prompt still forbidding what it was written to
forbid). They cannot judge phrasing -- no test without a model can.

The behaviours themselves -- "does it ask a question instead of picking a
t-shirt" -- are judgement, and judgement needs the model. Those live in
`test_conversation_live.py`, which runs against the real provider when a key
is present and skips when it is not.
"""

from __future__ import annotations

import pytest

from chatbot import agent
from chatbot import session as session_store
from chatbot.prompt import SYSTEM_PROMPT
from chatbot.providers.base import ModelReply
from chatbot.providers.fake import ScriptedProvider
from chatbot.tools.base import MAX_EXTRA_IMAGES, MAX_PRODUCT_IMAGES, ToolContext, call_tool

CHANNEL = "whatsapp"
WHO = "201000000001"
BLACK_XL = "cairokee-tee-xl-black"


def call(ctx, name, **arguments):
    return call_tool(ctx, name, arguments)


@pytest.fixture()
def ctx(seeded):
    return ToolContext(session=seeded, channel=CHANNEL, external_id=WHO)


# --------------------------------------------------------------------------
# H. Product images -- the one that was actually broken
# --------------------------------------------------------------------------


def test_product_photos_are_attached_not_just_described(ctx):
    """The bug: only the singular `image` key was collected, so size charts
    were attached and product photos never were. The model was handed the
    paths as data, announced it had sent pictures, and the reply carried
    none."""
    result = call(ctx, "get_variants", product_id="wanas-hoodie")
    assert result["images"], "fixture check: this product has photos"
    assert ctx.attachments, "get_variants returned photos but attached nothing"
    assert all(p.startswith("data/images/") for p in ctx.attachments)


def test_a_plain_request_sends_exactly_one_photo(ctx):
    """Showing the product is not a gallery: the default is one photo, full
    stop -- credit waste is the whole point of this rule."""
    call(ctx, "get_variants", product_id="wanas-hoodie")
    assert len(ctx.attachments) == 1


def test_more_images_prefers_colour_variety_and_is_capped(ctx):
    """An explicit "show me more" is the only way past the one-photo default,
    and even then it is capped -- two more, not the whole catalogue photo."""
    call(ctx, "get_variants", product_id="wanas-hoodie")
    first = list(ctx.attachments)
    call(ctx, "get_variants", product_id="wanas-hoodie", more_images=True)
    added = [p for p in ctx.attachments if p not in first]
    assert 0 < len(added) <= MAX_EXTRA_IMAGES

    colours = {
        colour
        for colour, paths in ctx.session.get(
            __import__("domain.models", fromlist=["Product"]).Product, "wanas-hoodie"
        ).color_images.items()
        for path in paths
        if path in ctx.attachments
    }
    assert len(colours) > 1


def test_product_photos_are_capped(ctx):
    call(ctx, "get_variants", product_id="wanas-hoodie")
    assert 0 < len(ctx.attachments) <= MAX_PRODUCT_IMAGES


def test_a_product_the_store_never_split_by_colour_still_gets_photos(ctx):
    """color_images is empty for five products; the unlabelled set is the
    honest fallback."""
    payload = call(ctx, "get_variants", product_id="feelin-fine-top")
    if payload["color_images"]:
        pytest.skip("this product is colour-split; covered by the test above")
    assert ctx.attachments
    assert ctx.attachments == payload["images"][:MAX_PRODUCT_IMAGES]


def test_size_charts_are_still_attached_alongside_product_photos(ctx):
    """The chart is the answer to a sizing question, so it is never dropped in
    favour of a product photo."""
    call(ctx, "get_variants", product_id="wanas-sweatpant")
    call(ctx, "get_size_chart", product_id="wanas-sweatpant")
    assert "data/size-charts/wide-leg-sweatpants.png" in ctx.attachments


def test_the_same_photo_is_never_attached_twice_in_a_turn(ctx):
    call(ctx, "get_variants", product_id="wanas-hoodie")
    first = list(ctx.attachments)
    call(ctx, "get_variants", product_id="wanas-hoodie")
    assert ctx.attachments == first


def test_attachments_reach_the_agent_reply(seeded):
    """End of the path the audit followed: tool -> context -> AgentReply."""
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "c", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie"}}]),
            ModelReply(text="ده الـ WANAS Hoodie، بييجي بـ٣ ألوان."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "وريني الهودي", provider=provider)
    assert reply.attachments
    assert all(p.startswith("data/images/") for p in reply.attachments)


def test_a_product_with_no_photos_attaches_nothing(ctx, monkeypatch):
    """Honest: no pictures means no pictures, not a claim that some were
    sent."""
    from domain.models import Product

    product = ctx.session.get(Product, "wanas-hoodie")
    product.images = []
    product.color_images = {}
    ctx.session.flush()

    call(ctx, "get_variants", product_id="wanas-hoodie")
    assert ctx.attachments == []


def test_the_prompt_forbids_claiming_an_image_that_was_not_sent():
    assert "متقولش «بعتلك الصور» غير لو انت فعلاً عرضت منتج" in SYSTEM_PROMPT
    assert "لو مفيش صور للمنتج، قول كده بصراحة" in SYSTEM_PROMPT


# --------------------------------------------------------------------------
# No path ever reaches a customer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "اتفضل الصورة data/images/wanas-black-hoodie/01.jpg",
        "شوف data\\size-charts\\zipup.png كده",
        "الصور هنا: data/images/x/01.jpg و data/images/x/02.jpg",
    ],
)
def test_a_leaked_path_is_stripped_before_sending(text):
    cleaned, leaked = agent.strip_paths(text)
    assert leaked is True
    assert "data/" not in cleaned and "data\\" not in cleaned


def test_ordinary_text_is_left_alone():
    text = "الأسود XL متاح بـ600 جنيه. تحب واحد؟"
    assert agent.strip_paths(text) == (text, False)


def test_the_agent_strips_a_path_the_model_echoed(seeded, caplog):
    provider = ScriptedProvider([ModelReply(text="اتفضل data/images/wanas-black-hoodie/01.jpg")])
    with caplog.at_level("WARNING"):
        reply = agent.run_turn(seeded, CHANNEL, WHO, "وريني", provider=provider)
    assert "data/" not in reply.text
    assert "stripped a file path" in caplog.text


# --------------------------------------------------------------------------
# B / E / I. A compound request resolves in one pass, with nothing re-asked
# --------------------------------------------------------------------------


def test_product_colour_size_and_quantity_in_one_message_needs_one_pass(seeded):
    """"عايز Cairokee T-shirt أسود XL واتنين" -- everything is present, so the
    machinery must let it land without a clarification round trip."""
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "a", "name": "get_variants", "arguments": {"product_id": "cairokee-tee"}},
                ]
            ),
            ModelReply(
                tool_calls=[
                    {"id": "b", "name": "add_to_cart", "arguments": {"variant_id": BLACK_XL, "quantity": 2}}
                ]
            ),
            ModelReply(text="تمام، اتنين Cairokee Tee أسود XL في الشنطة."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز Cairokee T-shirt أسود XL واتنين", provider=provider)

    assert reply.tool_calls == ["get_variants", "add_to_cart"]
    assert reply.error is None
    from domain.services import carts

    cart = carts.cart_payload(seeded, CHANNEL, WHO)
    assert cart["item_count"] == 2
    assert cart["lines"][0]["size"] == "XL"
    assert cart["lines"][0]["color"] == "Black"


def test_buying_intent_adds_without_a_second_confirmation(seeded):
    """"حطهولي" when the variant is already pinned down: one turn, no question."""
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "a", "name": "add_to_cart", "arguments": {"variant_id": BLACK_XL}}]),
            ModelReply(text="اتحطت 👌"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "خلاص حطهولي", provider=provider)
    assert reply.tool_calls == ["add_to_cart"]
    from domain.services import carts

    assert carts.cart_payload(seeded, CHANNEL, WHO)["item_count"] == 1


def test_a_variant_that_does_not_exist_is_refused_not_guessed(ctx):
    """"only ask for what is missing" must never become "invent what is
    missing" -- the tool still refuses an unresolvable id."""
    assert call(ctx, "add_to_cart", variant_id="cairokee-tee-xxl-black")["error"] == "variant_not_found"


# --------------------------------------------------------------------------
# C / D / F / G. Context resolution needs the history to actually be there
# --------------------------------------------------------------------------


def test_the_model_is_given_the_whole_recent_conversation(seeded):
    """"التاني" / "طب الأسود" / "أيوه" are only resolvable against what was
    just said, so the request has to carry it."""
    provider = ScriptedProvider([ModelReply(text="أ"), ModelReply(text="ب"), ModelReply(text="ج")])
    agent.run_turn(seeded, CHANNEL, WHO, "عايز حاجة للخروج", provider=provider)
    agent.run_turn(seeded, CHANNEL, WHO, "تيشيرت", provider=provider)
    agent.run_turn(seeded, CHANNEL, WHO, "التاني عجبني", provider=provider)

    sent_history = provider.calls[-1][1]
    texts = [m.get("content", "") for m in sent_history]
    assert "عايز حاجة للخروج" in texts
    assert "تيشيرت" in texts
    assert "التاني عجبني" in texts
    # ...and the assistant's own turns, which is what "التاني" refers back to.
    assert "أ" in texts and "ب" in texts


def test_context_survives_a_restart(seeded):
    """History is in the database, so "طب الأسود" still resolves after the
    process that heard the first half is gone."""
    provider = ScriptedProvider([ModelReply(text="عندنا Black و Olive")])
    agent.run_turn(seeded, CHANNEL, WHO, "الهودي بيجي بألوان إيه؟", provider=provider)
    seeded.commit()
    seeded.expire_all()

    resumed = ScriptedProvider([ModelReply(text="تمام، الأسود.")])
    agent.run_turn(seeded, CHANNEL, WHO, "طب الأسود", provider=resumed)
    texts = [m.get("content", "") for m in resumed.calls[0][1]]
    assert "الهودي بيجي بألوان إيه؟" in texts
    assert "عندنا Black و Olive" in texts


def test_tool_results_stay_in_the_history_the_model_reads(seeded):
    """"التاني" refers to options that came out of a tool result, so those
    results have to still be in the conversation on the next turn."""
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "a", "name": "get_products", "arguments": {"query": "tee"}}]),
            ModelReply(text="عندنا كذا تيشيرت"),
            ModelReply(text="تمام، التاني"),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "وريني تيشيرتات", provider=provider)
    agent.run_turn(seeded, CHANNEL, WHO, "التاني عجبني", provider=provider)

    roles = [m["role"] for m in provider.calls[-1][1]]
    assert "tool_results" in roles


# --------------------------------------------------------------------------
# A. The instructions that keep the bot from picking a product at random
# --------------------------------------------------------------------------


def test_the_prompt_forbids_answering_a_broad_request_with_one_product():
    assert "متختارش منتج من دماغك وتعرضه كإنه الإجابة" in SYSTEM_PROMPT
    assert "اسأل سؤال واحد خفيف يضيّق الاختيار" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "phrase",
    ["هل ترغب في", "هل تريد أن", "يرجى", "وجدت المنتجات التالية", "ماذا تريد أن أفعل؟"],
)
def test_the_prompt_names_the_robotic_phrases_it_bans(phrase):
    banned = SYSTEM_PROMPT.split("# أهم حاجة")[0]
    assert phrase in banned, "the ban list lost a phrase that was observed in real replies"


def test_the_prompt_covers_every_reference_word_that_was_observed():
    for reference in ("التاني", "الأول", "طب الأسود", "نفس المقاس", "أيوه", "تمام", "ماشي"):
        assert reference in SYSTEM_PROMPT


def test_the_prompt_still_carries_the_rules_that_are_not_negotiable():
    """Natural conversation must not have cost the guardrails."""
    for rule in (
        "متقولش سعر ولا مقاس ولا حاجة متوفرة من دماغك",
        "الـ variant_id بييجي من get_variants بس",
        "متقولش إن الأوردر اتعمل غير لما confirm_order يرجّع رقم أوردر",
        "الدفع كاش عند الاستلام بس",
        "مقاسات الهدوم وهي مفرودة",
        "الـ Tops مفيهاش XL",
        "request_human",
    ):
        assert rule in SYSTEM_PROMPT


def test_the_prompt_forbids_leaking_internals():
    assert "متذكرش أسماء الأدوات ولا الـ IDs" in SYSTEM_PROMPT
    assert "متكتبش أي مسار ملف ولا لينك أبداً" in SYSTEM_PROMPT


def test_the_prompt_did_not_become_a_wall_of_text():
    """A prompt nobody can hold in their head is one the model stops
    following. Kept in the range where the instructions still read as rules."""
    assert 3000 < len(SYSTEM_PROMPT) < 9000


# --------------------------------------------------------------------------
# Image credit waste: a photo already sent this conversation is not sent again
# --------------------------------------------------------------------------


def test_an_image_already_sent_this_conversation_is_not_sent_again(seeded):
    """"وريني الهودي" twice in the same conversation should not cost a second
    photo -- the customer already has it."""
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "a", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie"}}]),
            ModelReply(text="ده الـ WANAS Hoodie."),
            ModelReply(tool_calls=[{"id": "b", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie"}}]),
            ModelReply(text="زي ما قلتلك، ده هو."),
        ]
    )
    first = agent.run_turn(seeded, CHANNEL, WHO, "وريني الهودي", provider=provider)
    assert first.attachments

    second = agent.run_turn(seeded, CHANNEL, WHO, "وريني تاني", provider=provider)
    assert second.attachments == []


def test_sent_images_are_recorded_on_the_assistant_message(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "a", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie"}}]),
            ModelReply(text="اتفضل."),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "وريني الهودي", provider=provider)
    stored = session_store.load(seeded, CHANNEL, WHO)
    final = [m for m in stored if m["role"] == "assistant" and m.get("content")][-1]
    assert final["attachments"]


# --------------------------------------------------------------------------
# C. A tool call written out as text must never reach the customer
# --------------------------------------------------------------------------


def test_a_tool_call_written_as_text_is_stripped(seeded):
    provider = ScriptedProvider(
        [ModelReply(text="تمام، هحول لحد من الفريق. request_human(reason='unclear', summary='عايز حجم')")]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "مش فاهم", provider=provider)
    assert "request_human" not in reply.text
    assert "(" not in reply.text and ")" not in reply.text


def test_a_reply_that_is_only_a_leaked_tool_call_falls_back_gracefully(seeded):
    provider = ScriptedProvider([ModelReply(text="get_variants(product_id='wanas-hoodie')")])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "وريني الهودي", provider=provider)
    assert reply.text == agent.GENERIC_FAILURE


def test_strip_tool_leaks_leaves_ordinary_text_alone():
    text = "الأسود XL متاح بـ600 جنيه. تحب واحد؟"
    assert agent.strip_tool_leaks(text) == (text, False)


# --------------------------------------------------------------------------
# B. No AI-looking Markdown in a normal reply
# --------------------------------------------------------------------------


def test_bold_markdown_is_stripped_but_the_product_name_is_kept():
    cleaned, changed = agent.strip_markdown("عندنا **Boxy WNS Tee** بألوان كتير")
    assert changed is True
    assert cleaned == "عندنا Boxy WNS Tee بألوان كتير"


def test_a_markdown_heading_is_stripped():
    cleaned, changed = agent.strip_markdown("## المنتجات\nعندنا كذا تيشيرت")
    assert changed is True
    assert "#" not in cleaned


def test_the_agent_strips_markdown_the_model_produced(seeded):
    provider = ScriptedProvider([ModelReply(text="عندنا **Ringer Tee** دلوقتي بخصم")])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "وريني تيشيرت", provider=provider)
    assert "**" not in reply.text
    assert "Ringer Tee" in reply.text


# --------------------------------------------------------------------------
# E. A duplicate read-only call in the same conversation is served from
# what was already answered, not re-fetched
# --------------------------------------------------------------------------


def test_a_repeated_get_variants_call_is_not_re_fetched(seeded, monkeypatch):
    """"XL موجود؟" then "طب L؟" should not cost a second database read for the
    same product -- the answer is already in the conversation."""
    from chatbot.tools import catalog_tools

    calls = []
    original = catalog_tools.catalog.get_variants

    def counting(session, product_id):
        calls.append(product_id)
        return original(session, product_id)

    monkeypatch.setattr(catalog_tools.catalog, "get_variants", counting)

    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "a", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie"}}]),
            ModelReply(text="الـ XL موجود بس في Black."),
            ModelReply(tool_calls=[{"id": "b", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie"}}]),
            ModelReply(text="والـ L موجود في Black و Olive."),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "الهودي فيه XL؟", provider=provider)
    agent.run_turn(seeded, CHANNEL, WHO, "طب L؟", provider=provider)

    assert calls == ["wanas-hoodie"]  # the real read happened once, not twice


def test_a_call_for_a_different_product_is_not_served_from_cache(seeded, monkeypatch):
    from chatbot.tools import catalog_tools

    calls = []
    original = catalog_tools.catalog.get_variants

    def counting(session, product_id):
        calls.append(product_id)
        return original(session, product_id)

    monkeypatch.setattr(catalog_tools.catalog, "get_variants", counting)

    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "a", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie"}}]),
            ModelReply(text="أ"),
            ModelReply(tool_calls=[{"id": "b", "name": "get_variants", "arguments": {"product_id": "wanas-polo"}}]),
            ModelReply(text="ب"),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "الهودي؟", provider=provider)
    agent.run_turn(seeded, CHANNEL, WHO, "طب البولو؟", provider=provider)
    assert calls == ["wanas-hoodie", "wanas-polo"]


def test_a_tool_with_side_effects_is_never_cached(seeded):
    """add_to_cart must run for real every time -- caching a cart write would
    be a correctness bug, not an optimisation."""
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "a", "name": "add_to_cart", "arguments": {"variant_id": BLACK_XL}}]),
            ModelReply(text="واحد."),
            ModelReply(tool_calls=[{"id": "b", "name": "add_to_cart", "arguments": {"variant_id": BLACK_XL}}]),
            ModelReply(text="اتنين."),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "ضيف واحد", provider=provider)
    agent.run_turn(seeded, CHANNEL, WHO, "ضيف واحد كمان", provider=provider)

    from domain.services import carts

    assert carts.cart_payload(seeded, CHANNEL, WHO)["item_count"] == 2


# --------------------------------------------------------------------------
# F / G. A short reply continues the previous offer instead of escalating
# --------------------------------------------------------------------------


def test_the_prompt_treats_request_human_as_the_last_resort():
    section = SYSTEM_PROMPT.split("# التحويل لموظف")[1]
    assert "آخر حل" in section
    assert "أيوه" in section
