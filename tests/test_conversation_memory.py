"""What the bot still knows, and what it knows a reply is *about*.

Two failures the customer sees as one ("the bot is confused"), and they have
nothing to do with each other underneath:

* **it forgot.** `HISTORY_CAP` counted messages, and a single question costs
  four to six of them once the tool call and its results are counted -- so
  forty messages was seven or eight exchanges and the product discussed at the
  start of a conversation fell off the front of what the model was sent.
  `assistant/context.py` splits the window: recent messages verbatim, older
  ones compacted down to what was actually said.

* **it answered the wrong message.** A WhatsApp "reply to this" arrives as
  `context.id`, and the only thing that could ever be matched against it was
  the other messages in the same debounce batch. A reply to something the bot
  said was unresolvable outright -- nothing recorded the id WhatsApp gave a
  message the shop sent. `assistant/quoting.py` resolves it against the stored
  transcript now, and the ids come from `attach_outbound_ids`.
"""

from __future__ import annotations

from assistant import context, messages as msg, quoting, session as session_store
from assistant.agent import run_turn
from assistant.dispatcher import Pending
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider

CHANNEL = "whatsapp"
WHO = "201000000077"


def _exchange(question: str, answer: str, *, tool: str = "get_products") -> list[dict]:
    """One realistic turn's worth of history: four messages, three of which are
    the machinery underneath a single sentence."""
    return [
        msg.user(question),
        msg.assistant("", [{"id": "c", "name": tool, "arguments": {}}]),
        msg.tool_results([{"id": "c", "name": tool, "content": {"products": ["..."] * 40}}]),
        msg.assistant(answer),
    ]


# --- what the model is sent ------------------------------------------------


def test_a_short_conversation_is_sent_exactly_as_stored():
    history = _exchange("عندكم هودي؟", "أيوه، عندنا كذا لون")
    assert context.for_model(history) == history


def test_the_recent_window_is_verbatim_and_the_rest_is_compacted():
    history = _exchange("عندكم هودي أسود؟", "أيوه، Cairokee Hoodie أسود")
    history += _exchange("والشحن كام؟", "60 جنيه للقاهرة", tool="get_shipping_fee")

    sent = context.for_model(history, recent=4)

    # The turn the model is working in, untouched -- tool calls and all.
    assert sent[-4:] == history[-4:]
    # The one before it, down to the two sentences that were actually said.
    assert [m["role"] for m in sent[:-4]] == ["user", "assistant"]
    assert sent[0]["content"] == "عندكم هودي أسود؟"
    assert sent[1]["content"] == "أيوه، Cairokee Hoodie أسود"
    assert "tool_calls" not in sent[1]


def test_nothing_that_survives_compaction_is_reworded():
    """No summariser, on purpose. A compressed sentence that quietly loses
    'black' is worse than a shorter history."""
    history = []
    for n in range(10):
        history += _exchange(f"سؤال {n}", f"رد {n}")

    said = [m["content"] for m in context.for_model(history, recent=2) if m.get("content")]
    for text in said:
        assert text in [m.get("content") for m in history]


def test_the_verbatim_window_never_opens_on_an_orphaned_tool_result():
    """A `tool_results` whose call has been compacted away is the single most
    reliable way to have a whole request refused."""
    history = []
    for n in range(12):
        history += _exchange(f"سؤال {n}", f"رد {n}")

    for cap in range(1, 40):
        sent = context.for_model(history, recent=cap)
        for index, message in enumerate(sent):
            if message["role"] == "tool_results":
                assert index > 0 and sent[index - 1].get("tool_calls"), cap


def test_recall_is_bounded_but_far_longer_than_the_verbatim_window():
    history = []
    for n in range(20):
        history += _exchange(f"سؤال {n}", f"رد {n}")

    sent = context.for_model(history, recent=8, recall=10)
    assert len(sent) <= 8 + 10
    assert sent[0]["role"] == "user"


def test_recall_can_be_turned_off_entirely():
    history = _exchange("أول سؤال", "أول رد") + _exchange("تاني سؤال", "تاني رد")
    assert context.for_model(history, recent=4, recall=0) == history[-4:]


# --- (a) the product discussed early is still there ------------------------


def test_a_product_named_early_survives_a_long_conversation(seeded):
    """The customer's scenario: a product is discussed, several unrelated
    exchanges happen, then they ask a follow-up about that same product.

    Under the old single-cap window the opening message was long gone by the
    time the follow-up arrived, and the bot answered as though that part of
    the conversation had never happened.
    """
    provider = ScriptedProvider()
    opening = "عايز Cairokee Hoodie أسود مقاس سمول"

    provider.push(ModelReply(text="تمام، Cairokee Hoodie أسود سمول متاح"))
    run_turn(seeded, CHANNEL, WHO, opening, provider=provider)

    # Ten unrelated exchanges, each of them a real tool round trip -- which is
    # what actually eats the window.
    for n in range(10):
        provider.push(
            ModelReply(tool_calls=[{"id": f"c{n}", "name": "get_categories", "arguments": {}}])
        )
        provider.push(ModelReply(text=f"رد رقم {n}"))
        run_turn(seeded, CHANNEL, WHO, f"سؤال جانبي رقم {n}", provider=provider)

    provider.push(ModelReply(text="أيوه، لسه محجوز"))
    run_turn(seeded, CHANNEL, WHO, "طيب اللي كنا بنتكلم عنه ده، لسه متاح؟", provider=provider)

    # What the model was handed on that last call.
    _system, sent, _tools = provider.calls[-1]
    assert any(opening in (m.get("content") or "") for m in sent), (
        "the product the conversation opened with is no longer in the model's context"
    )


def test_the_stored_transcript_still_has_every_tool_exchange(seeded):
    """Compaction is a view. The dashboard and the archive are untouched."""
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "c", "name": "get_categories", "arguments": {}}]),
            ModelReply(text="عندنا كذا حاجة"),
        ]
    )
    run_turn(seeded, CHANNEL, WHO, "عندكم إيه؟", provider=provider)

    stored = session_store.transcript(seeded, CHANNEL, WHO)
    assert [m["role"] for m in stored] == ["user", "assistant", "tool_results", "assistant"]


# --- (b) the quoted message -----------------------------------------------


def test_a_reply_to_something_the_bot_said_is_resolved(seeded):
    """The case that could not work at all before: nothing recorded the id
    WhatsApp gave a message the shop sent, so a quote of it matched nothing."""
    provider = ScriptedProvider([ModelReply(text="عندنا أسود وأوليڤ وبيچ")])
    run_turn(seeded, CHANNEL, WHO, "الهودي بيجي بألوان إيه؟", provider=provider)

    # What the adapter does after Meta accepts the send.
    assert session_store.attach_outbound_ids(seeded, CHANNEL, WHO, ["wamid.bot.1"])

    provider.push(ModelReply(text="تمام، الأسود"))
    run_turn(seeded, CHANNEL, WHO, "الأول ده", provider=provider, reply_to=["wamid.bot.1"])

    _system, sent, _tools = provider.calls[-1]
    quoted = sent[-1]["content"]
    assert "عندنا أسود وأوليڤ وبيچ" in quoted
    assert "الأول ده" in quoted


def test_a_reply_to_the_customers_own_earlier_message_is_resolved(seeded):
    provider = ScriptedProvider([ModelReply(text="تمام")])
    run_turn(seeded, CHANNEL, WHO, "عايز التيشيرت الأبيض", provider=provider, mids=["wamid.in.1"])

    provider.push(ModelReply(text="ميديم من الأبيض"))
    run_turn(seeded, CHANNEL, WHO, "ميديم", provider=provider, reply_to=["wamid.in.1"])

    _system, sent, _tools = provider.calls[-1]
    assert "عايز التيشيرت الأبيض" in sent[-1]["content"]


def test_a_quote_of_something_no_longer_stored_is_answered_unannotated(seeded):
    """A reply to a message older than the transcript is still an ordinary
    message. Silence about which one it was beats inventing it."""
    provider = ScriptedProvider([ModelReply(text="تمام")])
    run_turn(seeded, CHANNEL, WHO, "ده", provider=provider, reply_to=["wamid.gone"])

    _system, sent, _tools = provider.calls[-1]
    assert sent[-1]["content"] == "ده"


def test_a_quote_resolves_against_the_archive_not_just_the_live_slice(seeded):
    provider = ScriptedProvider([ModelReply(text="عندنا أسود وأوليڤ")])
    run_turn(seeded, CHANNEL, WHO, "ألوان إيه؟", provider=provider)
    session_store.attach_outbound_ids(seeded, CHANNEL, WHO, ["wamid.bot.old"])

    # Six hours of silence, or a staff reset: the conversation ends for the
    # bot and nothing is deleted.
    session_store.clear(seeded, CHANNEL, WHO)
    assert session_store.load(seeded, CHANNEL, WHO) == []

    provider.push(ModelReply(text="الأسود متاح"))
    run_turn(seeded, CHANNEL, WHO, "ده", provider=provider, reply_to=["wamid.bot.old"])

    _system, sent, _tools = provider.calls[-1]
    assert "عندنا أسود وأوليڤ" in sent[-1]["content"]


def test_the_quoted_message_is_repeated_word_for_word(seeded):
    said = "المقاسات المتاحة: سمول، ميديم، لارج"
    provider = ScriptedProvider([ModelReply(text=said)])
    run_turn(seeded, CHANNEL, WHO, "مقاسات إيه؟", provider=provider)
    session_store.attach_outbound_ids(seeded, CHANNEL, WHO, ["wamid.sizes"])

    transcript = session_store.transcript(seeded, CHANNEL, WHO)
    annotated = quoting.annotate("ميديم", transcript, ["wamid.sizes"])
    assert said in annotated
    assert annotated.endswith("ميديم")


def test_outbound_ids_land_on_the_reply_and_not_on_the_customer(seeded):
    provider = ScriptedProvider([ModelReply(text="أهلاً")])
    run_turn(seeded, CHANNEL, WHO, "هاي", provider=provider)
    session_store.attach_outbound_ids(seeded, CHANNEL, WHO, ["wamid.a", "wamid.b"])

    stored = session_store.transcript(seeded, CHANNEL, WHO)
    assert stored[0]["role"] == "user" and "mids" not in stored[0]
    assert stored[-1]["mids"] == ["wamid.a", "wamid.b"]


def test_a_send_that_produced_no_id_changes_nothing(seeded):
    provider = ScriptedProvider([ModelReply(text="أهلاً")])
    run_turn(seeded, CHANNEL, WHO, "هاي", provider=provider)
    before = session_store.transcript(seeded, CHANNEL, WHO)

    assert session_store.attach_outbound_ids(seeded, CHANNEL, WHO, []) is False
    assert session_store.transcript(seeded, CHANNEL, WHO) == before


# --- the batch's own quotes still resolve without a database --------------


def test_a_reply_inside_the_batch_is_labelled_and_not_sent_on(seeded):
    """The debounce window can explain its own quotes; only what it cannot
    goes to the transcript lookup, so no quote is ever described twice."""
    pending = Pending(
        texts=["الأسود", "ولا الأوليڤ؟"],
        text_ids=["wamid.1", "wamid.2"],
        reply_to={"wamid.2": "wamid.1"},
    )
    assert 'their message "الأسود"' in pending.annotated_text()
    assert pending.unresolved_reply_to() == []


def test_a_quote_pointing_outside_the_batch_is_handed_on(seeded):
    pending = Pending(
        texts=["ميديم"], text_ids=["wamid.9"], reply_to={"wamid.9": "wamid.bot.earlier"}
    )
    assert pending.unresolved_reply_to() == ["wamid.bot.earlier"]
