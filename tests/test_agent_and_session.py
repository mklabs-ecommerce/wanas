"""The agent loop, session trimming, and the runtime's channel-independent rules.

Driven by the scripted provider: the real loop, the real session storage and
the real tools, with no network and nothing non-deterministic.
"""

from __future__ import annotations

from assistant import agent, messages as msg, session as session_store
from assistant.providers.base import ModelReply, ProviderError
from assistant.providers.fake import RehearsalProvider, ScriptedProvider
from assistant.runtime import handle_message, staff_reply
from config.settings import settings
from domain.models import ChannelIdentity, Order, QueueKind, SessionRow, ShippingRate, utcnow
from domain.services import (
    identities,
    notifications,
    queues,
)

CHANNEL = "whatsapp"
WHO = "201000000001"
VARIANT = "wanas-hoodie-s-olive"


def reply_with(name: str, arguments: dict | None = None) -> ModelReply:
    return ModelReply(tool_calls=[{"id": "c1", "name": name, "arguments": arguments or {}}])


# --- session trimming -----------------------------------------------------


def test_trim_keeps_the_cap_and_starts_at_a_user_message():
    history = []
    for n in range(30):
        history.append(msg.user(f"u{n}"))
        history.append(msg.assistant(f"a{n}"))

    trimmed = session_store.trim(history, cap=10)
    assert len(trimmed) <= 10
    assert trimmed[0]["role"] == "user"


def test_trim_never_splits_a_tool_call_from_its_result():
    """Cutting between a tool call and its result leaves the history malformed
    and providers reject the entire request."""
    history = []
    for n in range(12):
        history.append(msg.user(f"u{n}"))
        history.append(msg.assistant("", [{"id": "c", "name": "view_cart", "arguments": {}}]))
        history.append(msg.tool_results([{"id": "c", "name": "view_cart", "content": {"lines": []}}]))

    for cap in range(1, 40):
        trimmed = session_store.trim(history, cap=cap)
        assert trimmed[0]["role"] == "user", cap
        for index, message in enumerate(trimmed):
            if message["role"] == "tool_results":
                # Its assistant message must still be right in front of it.
                assert index > 0 and trimmed[index - 1].get("tool_calls"), cap


def test_trim_falls_back_to_the_last_user_message_when_the_tail_has_none():
    history = [msg.user("hello")]
    for _ in range(20):
        history.append(msg.assistant("", [{"id": "c", "name": "view_cart", "arguments": {}}]))
        history.append(msg.tool_results([{"id": "c", "name": "view_cart", "content": {}}]))

    trimmed = session_store.trim(history, cap=5)
    # More than the cap, deliberately: a fragment starting mid tool-call is
    # rejected outright, which is worse than a longer request.
    assert trimmed[0]["role"] == "user"
    assert len(trimmed) == len(history)


def test_history_is_capped_on_save(seeded):
    history = [msg.user(f"m{n}") for n in range(settings.history_cap + 20)]
    session_store.save(seeded, CHANNEL, WHO, history)
    stored = session_store.load(seeded, CHANNEL, WHO)
    assert len(stored) <= settings.history_cap


def test_session_expires_after_six_hours(seeded):
    from datetime import timedelta

    session_store.save(seeded, CHANNEL, WHO, [msg.user("hello")])
    row = seeded.get(SessionRow, (CHANNEL, WHO))
    row.updated_at = utcnow() - timedelta(hours=settings.session_expiry_hours + 1)
    seeded.flush()

    assert session_store.load(seeded, CHANNEL, WHO) == []


def test_session_survives_below_the_expiry_window(seeded):
    from datetime import timedelta

    session_store.save(seeded, CHANNEL, WHO, [msg.user("hello")])
    row = seeded.get(SessionRow, (CHANNEL, WHO))
    row.updated_at = utcnow() - timedelta(hours=settings.session_expiry_hours - 1)
    seeded.flush()
    assert len(session_store.load(seeded, CHANNEL, WHO)) == 1


# --- the agent loop -------------------------------------------------------


def test_a_reply_with_no_tool_calls_is_the_reply(seeded):
    provider = ScriptedProvider([ModelReply(text="أهلاً 👋")])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "هاي", provider=provider)
    assert reply.text == "أهلاً 👋"
    assert reply.tool_calls == []
    assert [m["role"] for m in session_store.load(seeded, CHANNEL, WHO)] == ["user", "assistant"]


def test_tool_results_are_fed_back_and_the_loop_continues(seeded):
    provider = ScriptedProvider(
        [reply_with("get_categories"), ModelReply(text="عندنا T-Shirts و Hoodies")]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عندكم إيه؟", provider=provider)

    assert reply.tool_calls == ["get_categories"]
    assert reply.text == "عندنا T-Shirts و Hoodies"
    roles = [m["role"] for m in session_store.load(seeded, CHANNEL, WHO)]
    assert roles == ["user", "assistant", "tool_results", "assistant"]
    # The second call really did see the tool result.
    assert provider.calls[1][1][-1]["role"] == "tool_results"


def test_all_tool_calls_in_one_turn_run_together(seeded):
    """"the black hoodie in L and the olive polo in M" is one message with two
    products, and should resolve in one pass."""
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {"id": "a", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie"}},
                    {"id": "b", "name": "get_variants", "arguments": {"product_id": "wanas-polo"}},
                ]
            ),
            ModelReply(text="الاتنين موجودين"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز الهودي والبولو", provider=provider)
    assert reply.tool_calls == ["get_variants", "get_variants"]
    assert len(provider.calls) == 2  # one round trip, not two


def test_the_loop_is_capped(seeded):
    provider = ScriptedProvider([reply_with("view_cart") for _ in range(settings.tool_loop_cap + 5)])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "…", provider=provider)
    assert reply.error == "loop_cap"
    assert len(reply.tool_calls) == settings.tool_loop_cap
    assert reply.text == agent.LOOP_EXHAUSTED


def test_attachments_are_collected_not_pasted_into_the_text(seeded):
    provider = ScriptedProvider(
        [
            reply_with("get_size_chart", {"product_id": "wanas-sweatpant"}),
            ModelReply(text="مقاس M: الوسط 34 سم، مقاس الهدوم مفرودة"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "مقاس M كام؟", provider=provider)
    assert reply.attachments == ["data/size-charts/wide-leg-sweatpants.png"]
    assert "data/size-charts" not in reply.text


def test_rate_limit_is_told_to_the_customer(seeded):
    class Limited(ScriptedProvider):
        def generate(self, *_a, **_k):
            raise ProviderError("429", kind="rate_limit")

    reply = agent.run_turn(seeded, CHANNEL, WHO, "هاي", provider=Limited())
    assert reply.text == agent.RATE_LIMITED
    assert reply.error == "rate_limit"


def test_auth_failure_is_generic_to_the_customer(seeded):
    class Broken(ScriptedProvider):
        def generate(self, *_a, **_k):
            raise ProviderError("bad key", kind="auth")

    reply = agent.run_turn(seeded, CHANNEL, WHO, "هاي", provider=Broken())
    assert reply.text == agent.GENERIC_FAILURE
    assert "bad key" not in reply.text  # no stack traces, no config detail
    assert reply.error == "auth"


def test_debug_flag_surfaces_the_real_error_and_defaults_off(seeded, monkeypatch):
    assert settings.chatbot_debug is False

    class Broken(ScriptedProvider):
        def generate(self, *_a, **_k):
            raise ProviderError("bad key", kind="auth")

    import dataclasses

    monkeypatch.setattr(agent, "settings", dataclasses.replace(settings, chatbot_debug=True))
    reply = agent.run_turn(seeded, CHANNEL, WHO, "هاي", provider=Broken())
    assert "bad key" in reply.text


def test_an_empty_model_reply_does_not_send_an_empty_message(seeded):
    provider = ScriptedProvider([ModelReply(text="", finish_reason="MAX_TOKENS")])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "هاي", provider=provider)
    assert reply.text == agent.GENERIC_FAILURE


def test_a_dangling_promise_is_retried_instead_of_sent(seeded):
    """A reply that promises to look something up ("هقولك المتاح بالظبط") with
    no tool call behind it must not reach the customer as-is -- the exact
    real-world bug where the bot said this and then the turn just ended."""
    provider = ScriptedProvider(
        [
            ModelReply(text="هقولك المتاح بالظبط"),
            reply_with("get_categories"),
            ModelReply(text="عندنا Joggers & Sweatpants"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "قولي ايه المتاح", provider=provider)
    assert reply.text == "عندنا Joggers & Sweatpants"
    assert reply.tool_calls == ["get_categories"]
    # The dangling promise itself never entered history -- only the real answer.
    roles = [m["role"] for m in session_store.load(seeded, CHANNEL, WHO)]
    assert roles == ["user", "assistant", "tool_results", "assistant"]


def test_a_model_that_keeps_promising_gets_the_fallback_not_dead_air(seeded):
    """The retry is a nudge, not a guarantee -- but a promise must never reach
    the customer. Nothing produces a second message for a turn, so a promise
    that is sent *is* the dead air. After the retries it becomes a question
    that stands on its own."""
    provider = ScriptedProvider(
        [
            ModelReply(text="هقولك دلوقتي"),
            ModelReply(text="ثواني هشوفلك"),
            ModelReply(text="لحظة هتأكدلك"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ايه المتاح", provider=provider)
    assert reply.text == agent.PROMISE_FALLBACK
    assert reply.error == "dangling_promise"
    assert len(provider.calls) == 3


def test_a_stock_check_promise_never_ends_the_turn(seeded):
    """The recurring production bug, in its real wording: the customer asks
    whether one colour is available, the model answers only "one second, I'll
    check for you" and calls nothing. The turn must not end there."""
    provider = ScriptedProvider(
        [
            ModelReply(text="تمام يا فندم، ثواني بس هشوفلك المتاح في اللون ده وأقولك"),
            reply_with("get_variants", {"product_id": "wanas-hoodie"}),
            ModelReply(text="الزيتي متاح في S و M بـ 750 جنيه"),
        ]
    )
    reply = agent.run_turn(
        seeded, CHANNEL, WHO, "الهودي الزيتي متاح؟", provider=provider
    )
    assert "get_variants" in reply.tool_calls
    assert not agent._is_dangling_promise(reply.text, tools_called=bool(reply.tool_calls))
    assert "750" in reply.text


def test_a_promise_after_a_tool_call_is_still_caught(seeded):
    """The variant the old detector could never see: the model *did* look
    something up and then still ended the turn on "I'll get back to you"."""
    provider = ScriptedProvider(
        [
            reply_with("get_categories"),
            ModelReply(text="ثانية واحدة وهراجعلك المخزن"),
            ModelReply(text="عندنا Joggers & Sweatpants متاحة دلوقتي"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ايه المتاح", provider=provider)
    assert reply.text == "عندنا Joggers & Sweatpants متاحة دلوقتي"


def test_promising_photos_and_attaching_none_is_retried(seeded):
    """The reported bug, in its exact shape: asked for every colour, the model
    replied that it was sending them and called nothing, so the turn ended
    with a confirmation and no picture. Twice in one conversation.

    The signal is structural, not verbal -- an empty attachment list -- because
    the sentence itself is perfectly well-formed and reads as success.
    """
    provider = ScriptedProvider(
        [
            ModelReply(text="تمام، هبعتلك صور كل الألوان دلوقتي"),
            reply_with("get_variants", {"product_id": "wanas-hoodie", "more_images": True}),
            ModelReply(text="دي كل الألوان المتاحة"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ابعتلي صور كل الألوان", provider=provider)
    assert reply.attachments, "the retry was supposed to produce real photos"
    assert reply.tool_calls == ["get_variants"]
    roles = [m["role"] for m in session_store.load(seeded, CHANNEL, WHO)]
    assert roles == ["user", "assistant", "tool_results", "assistant"]


def test_a_model_that_keeps_promising_photos_gets_a_question_not_a_lie(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(text="حاضر، هبعتلك الصور"),
            ModelReply(text="الصور جاية حالًا"),
            ModelReply(text="اتفضل الصور"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ابعتلي صور", provider=provider)
    assert reply.text == agent.IMAGE_PROMISE_FALLBACK
    assert reply.error == "image_promise"
    assert reply.attachments == []


def test_a_reply_that_really_attached_photos_is_not_touched(seeded):
    """The guard reads the attachments, so a reply that says it is sending
    pictures *while sending them* is the normal, correct case."""
    provider = ScriptedProvider(
        [
            reply_with("get_variants", {"product_id": "wanas-hoodie"}),
            ModelReply(text="اتفضل صورة الهودي"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ابعتلي صورة", provider=provider)
    assert reply.text == "اتفضل صورة الهودي"
    assert reply.attachments


def test_saying_a_product_has_no_photos_is_not_a_broken_promise(seeded):
    """The prompt asks for exactly this sentence when a product has none.
    Retrying it would push the model towards inventing a picture instead."""
    assert not agent._promises_images("معلش، مفيش صور للمنتج ده عندنا دلوقتي")
    assert not agent._promises_images("للأسف مفيش صور متاحة")


def test_answering_a_customers_own_photo_is_not_a_broken_promise(seeded):
    """The reply the media path exists to produce mentions the photo, because
    the photo is what the customer just sent. Guarding on the word alone would
    have retried it and then replaced it with an unrelated question."""
    answer = "وصلتني الصورة، وأقرب حاجة عندنا ليها هي الهودي الأسود"
    provider = ScriptedProvider([ModelReply(text=answer)])
    reply = agent.run_turn(
        seeded,
        CHANNEL,
        WHO,
        "قراءة آلية للصورة: أقرب منتج عندنا هو WANAS HOODIE.",
        provider=provider,
        images=["data/inbound/whatsapp/x.jpg"],
    )
    assert reply.text == answer
    assert len(provider.calls) == 1


def test_asking_which_photos_they_want_is_not_a_broken_promise(seeded):
    """A question leaves the customer something to answer, which is never dead
    air -- the same line the dangling-promise fallback is built on."""
    assert not agent._promises_images("تحب أبعتلك صور أنهي لون؟")


def test_a_real_answer_that_mentions_a_promise_word_is_sent_untouched(seeded):
    """The guard must not eat an actual answer. This one delivers the numbers
    and only then offers to follow up."""
    answer = (
        "الأسود متاح في L و XL بـ 850 جنيه، ولو حبيت أي لون تاني قولي وأشوفلك"
    )
    provider = ScriptedProvider([reply_with("get_categories"), ModelReply(text=answer)])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "الأسود؟", provider=provider)
    assert reply.text == answer


# --- runtime --------------------------------------------------------------


def test_paused_conversation_is_stored_not_answered(seeded):
    provider = ScriptedProvider([ModelReply(text="should never be produced")])
    identities.pause(seeded, CHANNEL, WHO)

    reply = handle_message(CHANNEL, WHO, "في حد؟", db=seeded, provider=provider)
    assert reply.paused is True
    assert reply.text is None
    assert provider.calls == []  # the model was not called at all

    # ...but staff can see what was said, and when.
    stored = session_store.load(seeded, CHANNEL, WHO)
    assert stored[-1]["role"] == "user"
    assert stored[-1]["content"] == "في حد؟"
    assert stored[-1]["at"]


def test_only_a_staff_action_un_pauses(seeded):
    identities.pause(seeded, CHANNEL, WHO)
    handle_message(CHANNEL, WHO, "لسه مستني", db=seeded, provider=ScriptedProvider())
    assert identities.is_paused(seeded, CHANNEL, WHO) is True

    identities.unpause(seeded, CHANNEL, WHO)
    reply = handle_message(
        CHANNEL, WHO, "تمام", db=seeded, provider=ScriptedProvider([ModelReply(text="أهلاً بيك تاني")])
    )
    assert reply.paused is False
    assert reply.text == "أهلاً بيك تاني"


def test_an_incoming_image_goes_straight_to_a_human(seeded):
    """The runtime raises it before the model sees anything -- the model is
    never handed an image, so it cannot classify one."""
    provider = ScriptedProvider([ModelReply(text="a hoodie, probably")])
    reply = handle_message(
        CHANNEL, WHO, "عايز زي كده", image_paths=["/tmp/photo.jpg"], db=seeded, provider=provider
    )

    assert provider.calls == []
    assert reply.paused is True
    assert "الفريق" in reply.text
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "image_received"
    assert item.payload["images"] == ["/tmp/photo.jpg"]
    assert identities.is_paused(seeded, CHANNEL, WHO) is True


def test_duplicate_webhook_delivery_is_ignored(seeded):
    provider = ScriptedProvider([ModelReply(text="أهلاً"), ModelReply(text="أهلاً تاني")])

    first = handle_message(CHANNEL, WHO, "هاي", platform_message_id="wamid.1", db=seeded, provider=provider)
    second = handle_message(CHANNEL, WHO, "هاي", platform_message_id="wamid.1", db=seeded, provider=provider)

    assert first.text == "أهلاً"
    assert second.duplicate is True
    assert second.text is None
    assert len(provider.calls) == 1


def test_staff_reply_lands_in_the_history_the_model_reads(seeded):
    identities.pause(seeded, CHANNEL, WHO)
    handle_message(CHANNEL, WHO, "فين طلبي؟", db=seeded, provider=ScriptedProvider())
    staff_reply(seeded, CHANNEL, WHO, "أهلاً، أنا مها من الفريق — طلبك خرج امبارح.")
    identities.unpause(seeded, CHANNEL, WHO)

    provider = ScriptedProvider([ModelReply(text="أي حاجة تانية؟")])
    handle_message(CHANNEL, WHO, "تمام شكراً", db=seeded, provider=provider)

    sent_history = provider.calls[0][1]
    assert any("مها" in (m.get("content") or "") for m in sent_history)


def test_first_contact_creates_an_identity_with_no_client(seeded):
    handle_message(CHANNEL, "20155555555", "هاي", db=seeded, provider=ScriptedProvider([ModelReply(text="أهلاً")]))
    identity = seeded.get(ChannelIdentity, (CHANNEL, "20155555555"))
    assert identity is not None
    assert identity.client_id is None


def test_blank_message_is_ignored(seeded):
    provider = ScriptedProvider([ModelReply(text="…")])
    reply = handle_message(CHANNEL, WHO, "   ", db=seeded, provider=provider)
    assert reply.text is None
    assert provider.calls == []


# --- end to end through the rehearsal provider ---------------------------


def test_full_order_through_the_harness_entry_point(seeded):
    """The whole flow with no LLM key: browse, variants, cart, shipping,
    order -- through handle_message, the same call WhatsApp makes."""
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.flush()
    provider = RehearsalProvider()

    def say(text: str, **kw):
        return handle_message(CHANNEL, WHO, text, db=seeded, provider=provider, **kw)

    assert "T-Shirts" in say("categories").text
    assert "wanas-hoodie" in say("products hoodie").text
    assert VARIANT in say("variants wanas-hoodie").text
    assert "الشنطة" in say(f"add {VARIANT} 2").text
    assert "60" in say("ship القاهرة").text

    reply = say("order Omar Ali | Cairo | 12 Test Street, Apt 4 | 01000000000")
    # The turn itself says nothing: the confirmation the customer reads is the
    # one `notifications.order_confirmed` composed and sent. A model reply on
    # top of it would be a second confirmation for the same order.
    assert reply.silent and not reply.text
    confirmation = notifications.get_sender(CHANNEL).sent[-1].text
    assert "1360" in confirmation  # 2 x 650 + 60

    order = seeded.get(Order, "WNS-1001")
    assert order.status == "Confirmed"
    assert order.items[0].quantity == 2
    assert seeded.get(SessionRow, (CHANNEL, WHO)) is not None

    assert "WNS-1001" in say("orders").text
    assert "710" in say(f"qty WNS-1001 {VARIANT} 1").text
    assert "اتلغى" in say("cancel WNS-1001").text


def test_sizing_question_sends_the_chart_image(seeded):
    reply = handle_message(
        CHANNEL, WHO, "size worker-jacket", db=seeded, provider=RehearsalProvider()
    )
    assert reply.attachments == ["data/size-charts/worker-jacket.png"]
    assert "مفرودة" in reply.text or "flat" in reply.text.lower()


# --- not knowing your size ------------------------------------------------


def _tool_results(db, name: str) -> list[dict]:
    """Every result stored for one tool, in order, straight from the session."""
    return [
        result["content"]
        for message in session_store.load(db, CHANNEL, WHO)
        if message["role"] == "tool_results"
        for result in message["results"]
        if result["name"] == name
    ]


def test_not_knowing_your_size_gets_the_chart_not_the_fallback(seeded):
    """The reported bug, end to end.

    The bot names a product, the customer says two messages later that they do
    not know their size, and the reply used to be the hardcoded fallback
    asking them to state the product they had just been told about. It read as
    amnesia; it was really the promise guard discarding a stalled reply and
    substituting a constant. The fix is upstream of the guard: the sizing
    question now has a tool that answers it, callable without a product_id the
    model may no longer have.
    """
    provider = ScriptedProvider(
        [
            reply_with("get_variants", {"product_id": "wanas-hoodie"}),
            ModelReply(text="WANAS Hoodie موجود، قولي اللون"),
            # No product_id: the customer's question never named one.
            reply_with("get_size_chart"),
            ModelReply(text="مقاس M عرضه 56 سم والطول 70 سم، قيسها على هدومك"),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "عايز أشوف الهودي", provider=provider)
    reply = agent.run_turn(seeded, CHANNEL, WHO, "مش عارف مقاسي، تقدر تساعدني؟", provider=provider)

    assert "get_size_chart" in reply.tool_calls
    assert reply.error is None
    assert reply.text != agent.PROMISE_FALLBACK
    # And the call resolved to the product under discussion rather than
    # erroring, which is what would have sent the model back to stalling.
    charts = _tool_results(seeded, "get_size_chart")
    assert charts and charts[-1].get("has_chart") is True


def test_a_size_chart_with_no_product_yet_asks_instead_of_guessing(seeded):
    """Nothing has been looked up, so there is nothing to resolve to. A
    distinct code, because the honest answer here is a question -- picking
    some product would be the wrong-chart failure this tool refuses."""
    provider = ScriptedProvider(
        [
            reply_with("get_size_chart"),
            ModelReply(text="تحب أشوفلك مقاسات أنهي منتج؟"),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "مش عارف مقاسي", provider=provider)
    assert _tool_results(seeded, "get_size_chart") == [{"error": "no_product_in_context"}]


def test_a_wrong_product_id_is_refused_never_resolved(seeded):
    """Only an *absent* id resolves. A wrong one is a different product the
    model meant, and answering it from context would quote a neighbouring
    garment's measurements -- confident, precise and wrong."""
    provider = ScriptedProvider(
        [
            reply_with("get_variants", {"product_id": "wanas-hoodie"}),
            ModelReply(text="WANAS Hoodie موجود"),
            reply_with("get_size_chart", {"product_id": "no-such-product"}),
            ModelReply(text="معلش مش لاقي المنتج ده"),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "عايز الهودي", provider=provider)
    agent.run_turn(seeded, CHANNEL, WHO, "المقاسات؟", provider=provider)
    assert _tool_results(seeded, "get_size_chart")[-1]["error"] == "product_not_found"


def test_an_implicit_chart_follows_the_product_not_the_first_one_asked(seeded):
    """`get_size_chart` is cached on its arguments, so an implicit call has to
    be made concrete *before* the cache sees it. Otherwise both of these are
    the call `{}` and the second customer question is answered with the first
    product's chart."""
    provider = ScriptedProvider(
        [
            reply_with("get_variants", {"product_id": "wanas-hoodie"}),
            ModelReply(text="WANAS Hoodie"),
            reply_with("get_size_chart"),
            ModelReply(text="أهو الجدول"),
            reply_with("get_variants", {"product_id": "ringer-tee"}),
            ModelReply(text="Ringer Tee"),
            reply_with("get_size_chart"),
            ModelReply(text="أهو جدول التيشيرت"),
        ]
    )
    for text in ("الهودي", "مقاساته؟", "طب الـ Ringer Tee", "ومقاساته؟"):
        agent.run_turn(seeded, CHANNEL, WHO, text, provider=provider)

    first, second = _tool_results(seeded, "get_size_chart")
    assert first["chart_id"] != second["chart_id"]


def test_the_last_resort_names_the_product_instead_of_forgetting_it(seeded):
    """When the model will not stop stalling the fallback still ships -- but
    asking a customer to restate a product the bot itself just named is the
    half that read as amnesia. It asks only for what is still missing."""
    provider = ScriptedProvider(
        [
            reply_with("get_variants", {"product_id": "wanas-hoodie"}),
            ModelReply(text="WANAS Hoodie موجود"),
            ModelReply(text="ثواني هشوفلك"),
            ModelReply(text="هقولك دلوقتي"),
            ModelReply(text="لحظة هتأكدلك"),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "عايز الهودي", provider=provider)
    reply = agent.run_turn(seeded, CHANNEL, WHO, "مقاس L متاح؟", provider=provider)

    assert reply.error == "dangling_promise"
    assert reply.text != agent.PROMISE_FALLBACK
    assert "WANAS Hoodie" in reply.text


def test_the_last_resort_stays_context_free_when_nothing_was_established(seeded):
    """A conversation that never named a product must not be told what it was
    about. No product looked up, no product in the sentence."""
    provider = ScriptedProvider(
        [
            ModelReply(text="ثواني هشوفلك"),
            ModelReply(text="هقولك دلوقتي"),
            ModelReply(text="لحظة هتأكدلك"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ايه المتاح", provider=provider)
    assert reply.text == agent.PROMISE_FALLBACK


def test_the_fallback_carries_the_colour_when_one_was_picked(seeded):
    """The colour is on the call, not in the result, so it survives the same
    read. Nothing left to ask for but the size."""
    history = [
        msg.user("عايز الهودي الزيتي"),
        msg.assistant(
            "",
            [{"id": "c1", "name": "get_variants", "arguments": {"product_id": "wanas-hoodie", "color": "Olive"}}],
        ),
        msg.tool_results(
            [{"id": "c1", "name": "get_variants", "content": {"product_id": "wanas-hoodie", "name": "WANAS Hoodie"}}]
        ),
    ]
    assert agent.promise_fallback(history) == agent.PROMISE_FALLBACK_WITH_COLOR.format(
        product="WANAS Hoodie", color="Olive"
    )


def test_a_failed_lookup_is_not_what_the_conversation_is_about(seeded):
    """An errored tool result names a product that does not exist. Reading it
    back to the customer would be the fallback inventing context."""
    history = [
        msg.user("عايز حاجة"),
        msg.assistant("", [{"id": "c1", "name": "get_variants", "arguments": {"product_id": "ghost"}}]),
        msg.tool_results([{"id": "c1", "name": "get_variants", "content": {"error": "product_not_found"}}]),
    ]
    assert agent.promise_fallback(history) == agent.PROMISE_FALLBACK
