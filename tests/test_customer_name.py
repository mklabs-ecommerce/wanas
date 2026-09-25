"""The bot asks the customer's name once, saves it, and staff see it.

Before this the only name anywhere was `Client.full_name`, written at
checkout, so every conversation that had not reached an order was titled by a
phone number (or an Instagram handle) on the dashboard. What is pinned here:

* whether a turn asks is decided in code -- never when the name is known,
  once ever, and only near the start of a conversation;
* the name saved is one the customer typed (`save_customer_name` refuses
  anything else, the same way `confirm_order` refuses a phone number they
  never sent);
* the dashboard titles the conversation with it, list and thread alike.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import agent, customer_name, messages as msg, session as session_store
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.tools.base import ToolContext, call_tool, load_all
from config.settings import settings
from dashboard import inbox_api, web as dashboard
from dashboard.web import customer_labels
from domain.models import Client
from domain.services import auth, identities

load_all()

CHANNEL = "whatsapp"
WHO = "201000000077"
SECRET = "test-dashboard-secret"


def _ctx(db, *said: str) -> ToolContext:
    return ToolContext(
        session=db,
        channel=CHANNEL,
        external_id=WHO,
        history=[msg.user(text) for text in said],
    )


# --------------------------------------------------------------------------
# the tool
# --------------------------------------------------------------------------


def test_a_name_the_customer_typed_is_saved(seeded):
    ctx = _ctx(seeded, "اسمي أحمد")
    assert call_tool(ctx, "save_customer_name", {"name": "أحمد"}) == {"saved": True, "name": "أحمد"}
    assert identities.get(seeded, CHANNEL, WHO).customer_name == "أحمد"


def test_spelling_is_folded_the_way_search_folds_it(seeded):
    """«احمد» typed, «أحمد» written by the model: the same name."""
    ctx = _ctx(seeded, "انا احمد")
    assert call_tool(ctx, "save_customer_name", {"name": "أحمد"})["saved"] is True


def test_a_latin_name_matches_regardless_of_case(seeded):
    ctx = _ctx(seeded, "i'm mona")
    assert call_tool(ctx, "save_customer_name", {"name": "Mona"})["saved"] is True


def test_a_name_the_customer_never_typed_is_refused(seeded):
    ctx = _ctx(seeded, "عايز هودي أسود")
    result = call_tool(ctx, "save_customer_name", {"name": "أحمد"})
    assert result["error"] == "name_not_given"
    assert identities.get(seeded, CHANNEL, WHO) is None or not identities.get(
        seeded, CHANNEL, WHO
    ).customer_name


def test_a_name_only_the_bot_said_is_refused(seeded):
    ctx = _ctx(seeded, "أهلاً")
    ctx.history.append(msg.assistant("أهلاً يا أحمد"))
    assert call_tool(ctx, "save_customer_name", {"name": "أحمد"})["error"] == "name_not_given"


def test_a_surname_nobody_gave_is_refused(seeded):
    ctx = _ctx(seeded, "اسمي أحمد")
    assert call_tool(ctx, "save_customer_name", {"name": "أحمد محمود"})["error"] == "name_not_given"


@pytest.mark.parametrize(
    "bad",
    ["", "01001234567", "https://example.com", "أنا عايز هودي أسود مقاس لارج لو سمحت"],
)
def test_what_is_not_a_name_is_refused(seeded, bad):
    ctx = _ctx(seeded, bad or "x")
    assert call_tool(ctx, "save_customer_name", {"name": bad})["error"] in {
        "not_a_name",
        "bad_arguments",
    }


def test_a_correction_replaces_the_name(seeded):
    ctx = _ctx(seeded, "اسمي أحمد", "لا معلش اسمي محمد")
    call_tool(ctx, "save_customer_name", {"name": "أحمد"})
    call_tool(ctx, "save_customer_name", {"name": "محمد"})
    assert identities.get(seeded, CHANNEL, WHO).customer_name == "محمد"


def test_the_profile_carries_the_chat_name(seeded):
    ctx = _ctx(seeded, "اسمي سارة")
    call_tool(ctx, "save_customer_name", {"name": "سارة"})
    assert call_tool(ctx, "get_my_profile", {})["name"] == "سارة"


# --------------------------------------------------------------------------
# whether a turn asks
# --------------------------------------------------------------------------


def test_a_new_customer_is_asked(seeded):
    assert customer_name.should_ask(seeded, CHANNEL, WHO, []) is True
    assert "save_customer_name" in customer_name.turn_note(seeded, CHANNEL, WHO, [])


def test_a_saved_name_is_never_asked_for_again(seeded):
    identities.set_customer_name(seeded, CHANNEL, WHO, "أحمد")
    assert customer_name.should_ask(seeded, CHANNEL, WHO, []) is False
    note = customer_name.turn_note(seeded, CHANNEL, WHO, [])
    assert "«أحمد»" in note and "متسألوش" in note


def test_a_returning_customer_with_an_order_is_not_asked(seeded):
    client = Client(full_name="Mona Ali", phone="01000000077", address="x", governorate="cairo")
    seeded.add(client)
    seeded.flush()
    identity = identities.get_or_create(seeded, CHANNEL, WHO)
    identities.link(seeded, identity, client.client_id)
    assert customer_name.should_ask(seeded, CHANNEL, WHO, []) is False
    assert "Mona Ali" in customer_name.turn_note(seeded, CHANNEL, WHO, [])


def test_a_customer_who_ignored_the_question_is_not_asked_twice(seeded):
    history = [msg.user("السلام عليكم"), msg.assistant("وعليكم السلام، ممكن أعرف اسم حضرتك؟")]
    session_store.save(seeded, CHANNEL, WHO, history)
    assert customer_name.should_ask(seeded, CHANNEL, WHO, history) is False
    assert customer_name.turn_note(seeded, CHANNEL, WHO, history) == ""


def test_the_question_is_only_an_introduction(seeded):
    """Deep into choosing a size, «what's your name?» is a form field."""
    history = []
    for _ in range(customer_name.ASK_WITHIN_REPLIES):
        history += [msg.user("طب والأسود؟"), msg.assistant("متوفر في M و L")]
    assert customer_name.should_ask(seeded, CHANNEL, WHO, history) is False


@pytest.mark.parametrize(
    "reply",
    [
        "ممكن أعرف اسم حضرتك؟",
        "وبالمناسبة، اسمك إيه؟",
        "أتشرف باسم حضرتك",
        "May I have your name?",
    ],
)
def test_the_ways_a_reply_asks(reply):
    assert customer_name.asks_for_name(reply)


@pytest.mark.parametrize(
    "reply",
    ["اسم المنتج WANAS Hoodie", "نسجل الأوردر باسم أحمد؟", "متوفر في M و L"],
)
def test_what_is_not_asking(reply):
    assert not customer_name.asks_for_name(reply)


def test_the_turn_carries_the_note_to_the_model(seeded):
    provider = ScriptedProvider([ModelReply(text="أهلاً بحضرتك في Wanas Gallery. ممكن أعرف اسم حضرتك؟")])
    agent.run_turn(seeded, CHANNEL, WHO, "السلام عليكم", provider=provider)
    assert "لسه منعرفش اسم الزبون" in provider.calls[0][0]

    # Asked once; the next turn does not carry the request again.
    provider = ScriptedProvider([ModelReply(text="تمام")])
    agent.run_turn(seeded, CHANNEL, WHO, "عايز هودي", provider=provider)
    assert "لسه منعرفش اسم الزبون" not in provider.calls[0][0]


def test_the_answer_is_saved_and_named_from_then_on(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[{"id": "n1", "name": "save_customer_name", "arguments": {"name": "كريم"}}]
            ),
            ModelReply(text="أهلاً يا كريم، تحب أساعدك في إيه؟"),
        ]
    )
    agent.run_turn(seeded, CHANNEL, WHO, "كريم", provider=provider)
    assert identities.known_name(seeded, CHANNEL, WHO) == "كريم"

    provider = ScriptedProvider([ModelReply(text="تمام")])
    agent.run_turn(seeded, CHANNEL, WHO, "عايز هودي", provider=provider)
    assert "اسم الزبون «كريم»" in provider.calls[0][0]


# --------------------------------------------------------------------------
# the dashboard
# --------------------------------------------------------------------------


def test_the_chat_name_titles_a_conversation_with_no_order():
    labels = customer_labels(None, CHANNEL, WHO, None, "أحمد")
    assert labels["display_name"] == "أحمد"
    assert labels["customer_name"] == "أحمد"
    # The number is still there, beside it rather than instead of it.
    assert labels["customer_phone"] == WHO


def test_the_order_name_still_outranks_the_chat_name():
    client = Client(full_name="Ahmed Samir", phone="01000000077", address="", governorate=None)
    assert customer_labels(client, CHANNEL, WHO, None, "أحمد")["display_name"] == "Ahmed Samir"


@pytest.fixture()
def logged_in(monkeypatch, seeded):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(inbox_api.router)
    client = TestClient(app)
    auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


def test_staff_see_the_name_in_the_list_and_the_thread(logged_in, seeded):
    session_store.save(seeded, CHANNEL, WHO, [msg.user("اسمي أحمد"), msg.assistant("أهلاً يا أحمد")])
    identities.set_customer_name(seeded, CHANNEL, WHO, "أحمد")
    seeded.commit()

    listed = logged_in.get("/dashboard/api/conversations").json()["conversations"]
    assert [c["display_name"] for c in listed if c["external_id"] == WHO] == ["أحمد"]

    inbox = logged_in.get("/dashboard/api/inbox").json()["conversations"]
    assert [c["display_name"] for c in inbox if c["external_id"] == WHO] == ["أحمد"]

    thread = logged_in.get(f"/dashboard/api/conversations/{CHANNEL}/{WHO}").json()
    assert thread["display_name"] == "أحمد"
    assert thread["customer_name"] == "أحمد"


def test_the_inbox_search_finds_a_customer_by_their_chat_name(logged_in, seeded):
    session_store.save(seeded, CHANNEL, WHO, [msg.user("مساء الخير")])
    identities.set_customer_name(seeded, CHANNEL, WHO, "Karim")
    seeded.commit()
    found = logged_in.get("/dashboard/api/inbox", params={"q": "karim"}).json()["conversations"]
    assert [c["external_id"] for c in found] == [WHO]
