"""The Instagram comment screen: what was said, and what was said back.

The ledger row existed from the start, but only as an idempotency latch --
comment id, two ticks, nothing else. So the only record of a public comment
was an alert summary truncated to 200 characters, and only for the two
categories that raise one. A compliment, a size question or an FAQ answer
left no trace at all, and nobody could see what the shop had published under
its own posts. These pin the record and the way it is sorted.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import inbox_api, web as dashboard
from domain.models import InstagramCommentReply, utcnow
from domain.services import auth

SECRET = "a-test-secret-that-is-long-enough"


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


def _comment(session, comment_id, **kwargs):
    row = InstagramCommentReply(
        comment_id=comment_id,
        media_id="media-1",
        commenter_igsid=kwargs.pop("commenter_igsid", "ig-1"),
        created_at=utcnow(),
        **kwargs,
    )
    session.add(row)
    session.commit()
    return row


def _fetch(client, **params) -> dict:
    res = client.get("/dashboard/api/inbox/comments", params=params)
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------
# The record itself
# --------------------------------------------------------------------------


def test_a_comment_carries_its_words_and_both_replies(logged_in, seeded):
    _comment(
        seeded,
        "c1",
        text="بكام الهودي ده؟",
        category="price",
        sentiment="question",
        public_replied=True,
        public_reply_text="بعتنالك السعر في الخاص 🤍",
        private_replied=True,
        private_reply_text="أهلاً! السعر...",
        commenter_username="karim.ebrahiim",
    )

    (item,) = _fetch(logged_in)["comments"]
    assert item["text"] == "بكام الهودي ده؟"
    assert item["category"] == "price"
    assert item["sentiment"] == "question"
    assert item["public_reply"] == "بعتنالك السعر في الخاص 🤍"
    assert item["private_reply"] == "أهلاً! السعر..."
    assert item["commenter_handle"] == "@karim.ebrahiim"
    assert item["unanswered"] is False


def test_a_row_from_before_the_columns_existed_reads_as_unclassified(logged_in, seeded):
    """Every comment handled before this shipped has no text and no category.
    It must still list -- showing it as unclassified is the truth; guessing a
    sentiment for it would be inventing a record of a public surface."""
    _comment(seeded, "old", public_replied=True)

    (item,) = _fetch(logged_in)["comments"]
    assert item["text"] is None
    assert item["category"] is None
    assert item["sentiment"] == "other"


def test_a_comment_answered_with_nothing_is_flagged(logged_in, seeded):
    """The row worth finding: seen, classified, and met with silence. Either
    the DM budget was spent, a send failed, or the category has no
    customer-visible action at all."""
    _comment(seeded, "c2", text="وحش", category="negative", sentiment="negative")

    (item,) = _fetch(logged_in)["comments"]
    assert item["unanswered"] is True


def test_a_public_reply_alone_is_not_unanswered(logged_in, seeded):
    _comment(seeded, "c3", text="جميل", category="positive", sentiment="positive",
             public_replied=True, public_reply_text="نورتنا 🤍")
    assert _fetch(logged_in)["comments"][0]["unanswered"] is False


# --------------------------------------------------------------------------
# Sorting it
# --------------------------------------------------------------------------


@pytest.fixture()
def a_mixed_bag(seeded):
    _comment(seeded, "q1", text="بكام؟", category="price", sentiment="question")
    _comment(seeded, "n1", text="أسوأ محل", category="negative", sentiment="negative")
    _comment(seeded, "n2", text="الأوردر متأخر", category="complaint", sentiment="negative")
    _comment(seeded, "p1", text="تحفة", category="positive", sentiment="positive",
             public_replied=True, public_reply_text="نورتنا")
    _comment(seeded, "s1", text="follow me", category="spam", sentiment="other")
    return seeded


def test_the_counts_describe_the_whole_window(logged_in, a_mixed_bag):
    counts = _fetch(logged_in)["counts"]
    assert counts["all"] == 5
    assert counts["negative"] == 2
    assert counts["question"] == 1
    assert counts["positive"] == 1
    assert counts["other"] == 1
    assert counts["unanswered"] == 4


def test_filtering_narrows_the_rows_but_not_the_counts(logged_in, a_mixed_bag):
    """Counted before the filter on purpose: counting after would make every
    tab but the open one read zero, so the badges would be useless exactly
    when a person is using them."""
    data = _fetch(logged_in, sentiment="negative")
    assert {c["comment_id"] for c in data["comments"]} == {"n1", "n2"}
    assert data["counts"]["all"] == 5
    assert data["counts"]["positive"] == 1


def test_the_unanswered_cut_is_not_a_sentiment(logged_in, a_mixed_bag):
    data = _fetch(logged_in, sentiment="unanswered")
    assert {c["comment_id"] for c in data["comments"]} == {"q1", "n1", "n2", "s1"}


def test_an_unknown_filter_is_ignored_rather_than_emptying_the_list(logged_in, a_mixed_bag):
    assert len(_fetch(logged_in, sentiment="not-a-bucket")["comments"]) == 5


def test_search_looks_at_the_comment_and_at_the_reply(logged_in, a_mixed_bag):
    assert {c["comment_id"] for c in _fetch(logged_in, q="متأخر")["comments"]} == {"n2"}
    # ...and at what the shop said back, which is the half a person searching
    # for "what did we tell people about X" actually needs.
    assert {c["comment_id"] for c in _fetch(logged_in, q="نورتنا")["comments"]} == {"p1"}


def test_the_window_still_bounds_the_list(logged_in, seeded):
    old = _comment(seeded, "ancient", text="قديم", category="positive", sentiment="positive")
    old.created_at = utcnow() - timedelta(days=90)
    seeded.commit()

    assert _fetch(logged_in, days=30)["comments"] == []
    assert len(_fetch(logged_in, days=365)["comments"]) == 1


def test_comments_still_require_login(client, seeded):
    assert client.get("/dashboard/api/inbox/comments").status_code == 401
