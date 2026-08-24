"""The unified inbox: search, filters, per-filter counts, comment ledger.

`dashboard/web.py`'s `/api/conversations` is tested by `test_dashboard.py`
and is unchanged. These tests cover only what `dashboard/inbox_api.py` adds
on top of it -- and, most importantly, that it keeps the one promise the
original makes: a conversation waiting on a person is at the top of the list,
longest wait first, whatever else is in it.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import messages as msg, session as session_store
from config.settings import settings
from dashboard import inbox_api, web as dashboard
from domain.models import InstagramCommentReply
from domain.services import auth, identities

SECRET = "test-dashboard-secret"


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(inbox_api.router)
    return TestClient(app)


@pytest.fixture()
def logged_in(client, seeded):
    auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


def _conversation(session, channel, external_id, *, texts):
    identities.get_or_create(session, channel, external_id)
    history = []
    for who, text in texts:
        history.append(msg.user(text) if who == "user" else msg.assistant(text))
    session_store.save(session, channel, external_id, history)
    session.commit()


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


def test_inbox_requires_login(client):
    assert client.get("/dashboard/api/inbox").status_code == 401


def test_comments_require_login(client):
    assert client.get("/dashboard/api/inbox/comments").status_code == 401


# --------------------------------------------------------------------------
# listing, counts, filters
# --------------------------------------------------------------------------


def test_inbox_lists_every_channel_with_counts(logged_in, seeded):
    _conversation(seeded, "whatsapp", "201000000001", texts=[("user", "عندكم هودي؟")])
    _conversation(seeded, "instagram_dm", "ig-1", texts=[("user", "hi"), ("bot", "أهلاً")])

    body = logged_in.get("/dashboard/api/inbox").json()

    assert body["counts"]["all"] == 2
    assert body["by_channel"] == {"whatsapp": 1, "instagram_dm": 1}
    # The one whose last word is the customer's is the one nobody answered.
    assert body["counts"]["unanswered"] == 1


def test_inbox_filters_by_channel(logged_in, seeded):
    _conversation(seeded, "whatsapp", "201000000001", texts=[("user", "هاي")])
    _conversation(seeded, "instagram_dm", "ig-1", texts=[("user", "hey")])

    body = logged_in.get("/dashboard/api/inbox?channel=instagram_dm").json()

    assert [c["external_id"] for c in body["conversations"]] == ["ig-1"]
    # Counts stay store-wide: a filtered list must not make the tabs lie
    # about what is behind them.
    assert body["counts"]["all"] == 2


def test_inbox_searches_message_text(logged_in, seeded):
    _conversation(seeded, "whatsapp", "201000000001", texts=[("user", "عايز تيشيرت أوفرسايز")])
    _conversation(seeded, "whatsapp", "201000000002", texts=[("user", "فين الطلب بتاعي")])

    body = logged_in.get("/dashboard/api/inbox?q=أوفرسايز").json()

    assert [c["external_id"] for c in body["conversations"]] == ["201000000001"]


def test_inbox_searches_the_external_id_too(logged_in, seeded):
    _conversation(seeded, "whatsapp", "201555000111", texts=[("user", "هاي")])
    _conversation(seeded, "whatsapp", "201999000222", texts=[("user", "هاي")])

    body = logged_in.get("/dashboard/api/inbox?q=555").json()

    assert [c["external_id"] for c in body["conversations"]] == ["201555000111"]


def test_paused_conversations_come_first(logged_in, seeded):
    _conversation(seeded, "whatsapp", "201000000001", texts=[("user", "قديمة")])
    _conversation(seeded, "whatsapp", "201000000002", texts=[("user", "أحدث")])
    identities.pause(seeded, "whatsapp", "201000000001")
    seeded.commit()

    body = logged_in.get("/dashboard/api/inbox").json()

    assert body["conversations"][0]["external_id"] == "201000000001"
    assert body["conversations"][0]["paused"] is True
    assert body["counts"]["paused"] == 1


def test_needs_reply_excludes_a_manual_takeover(logged_in, seeded):
    """A staff member who took a conversation over is not waiting on
    themselves -- only a conversation the *bot* escalated is a request."""
    _conversation(seeded, "whatsapp", "201000000001", texts=[("user", "هاي")])
    identities.pause(seeded, "whatsapp", "201000000001")
    seeded.commit()

    body = logged_in.get("/dashboard/api/inbox").json()

    assert body["counts"]["paused"] == 1
    assert body["counts"]["needs_reply"] == 0


def test_bad_status_is_refused(logged_in):
    res = logged_in.get("/dashboard/api/inbox?status=whatever")
    assert res.status_code == 400
    assert res.json()["error"] == "bad_arguments"


def test_last_role_marks_who_spoke_last(logged_in, seeded):
    _conversation(seeded, "whatsapp", "201000000001", texts=[("user", "هاي"), ("bot", "أهلاً بيك")])

    body = logged_in.get("/dashboard/api/inbox").json()

    assert body["conversations"][0]["last_role"] == "bot"
    assert body["conversations"][0]["message_count"] == 2


# --------------------------------------------------------------------------
# the Instagram comment ledger
# --------------------------------------------------------------------------


def test_comment_ledger_reports_how_each_comment_was_handled(logged_in, seeded):
    seeded.add(
        InstagramCommentReply(
            comment_id="c1", media_id="m1", commenter_igsid="ig-9",
            public_replied=True, private_replied=True,
        )
    )
    seeded.add(
        InstagramCommentReply(
            comment_id="c2", media_id="m1", commenter_igsid="ig-8",
            public_replied=False, private_replied=False,
        )
    )
    seeded.commit()

    body = logged_in.get("/dashboard/api/inbox/comments").json()

    assert len(body["comments"]) == 2
    assert body["public_replies"] == 1
    assert body["private_replies"] == 1
    # A comment with neither reply is a real outcome (seen, deliberately not
    # engaged with), not a missing row.
    untouched = next(c for c in body["comments"] if c["comment_id"] == "c2")
    assert untouched["public_replied"] is False
    assert untouched["private_replied"] is False
