"""Comments (STEP 11) -- shipped with INSTAGRAM_COMMENTS_ENABLED=0.

These tests enable the flag explicitly; production defaults to off. The
first test in the file is the one the plan says to write first: the shop's
own comment, which must never be answered -- answering it is the bot
replying to itself publicly, forever.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot.channels import instagram as adapter
from config.settings import settings
from domain.db import SessionLocal, session_scope
from domain.models import (
    InstagramCommentReply,
    QueueKind,
    SessionRow,
    utcnow,
)
from domain.services import queues

APP_SECRET = "ig-test-app-secret"
VERIFY_TOKEN = "ig-verify-token"
IG_ID = "17841400000000000"
COMMENTER = "555000111222333"
COMMENT_ID = "17900000000000001"
MEDIA_ID = "media-1"

GRAPH = "https://graph.instagram.com"


@pytest.fixture()
def comments_on(monkeypatch):
    """Credentials + the comments flag on, for these tests only."""
    patched = dataclasses.replace(
        settings,
        instagram_account_id=IG_ID,
        instagram_access_token="test-token",
        instagram_app_secret=APP_SECRET,
        instagram_verify_token=VERIFY_TOKEN,
        instagram_comments_enabled=True,
        instagram_comment_max_age_hours=48.0,
        instagram_comment_rate_limit=3,
    )
    monkeypatch.setattr(adapter, "settings", patched)
    monkeypatch.setattr(
        __import__("integrations.instagram.client", fromlist=["settings"]),
        "settings",
        patched,
    )
    return patched


@pytest.fixture()
def client(seeded, comments_on):
    app = FastAPI()
    app.include_router(adapter.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def fake_graph(monkeypatch):
    from tests.fake_instagram import FakeInstagram

    fake = FakeInstagram()
    fake.post_body = {"recipient_id": COMMENTER}  # a private reply returns the IGSID
    return fake.install(monkeypatch)


def comment_body(
    text="بكام ده؟",
    *,
    comment_id=COMMENT_ID,
    commenter=COMMENTER,
    media_id=MEDIA_ID,
    parent_id=None,
    timestamp=None,
):
    value = {
        "id": comment_id,
        "text": text,
        "from": {"id": commenter, "username": "someone"},
        "media": {"id": media_id, "media_product_type": "FEED"},
    }
    if parent_id:
        value["parent_id"] = parent_id
    if timestamp:
        value["timestamp"] = timestamp
    return {
        "object": "instagram",
        # A live delivery carries Meta's *current* epoch; a fixed one would
        # make every comment look months old to the age filter.
        "entry": [
            {
                "id": IG_ID,
                "time": int(datetime.now(UTC).timestamp()),
                "changes": [{"field": "comments", "value": value}],
            }
        ],
    }


def post_comment(client, body):
    raw = __import__("json").dumps(body).encode()
    import hashlib
    import hmac as hmac_mod

    digest = hmac_mod.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/instagram",
        content=raw,
        headers={"content-type": "application/json", "x-hub-signature-256": f"sha256={digest}"},
    )


def public_replies(fake_graph):
    return [c for c in fake_graph.posts if c["url"].endswith("/replies")]


def private_replies(fake_graph, comment_id=None):
    out = []
    for c in fake_graph.posts:
        if not c["url"].endswith("/messages"):
            continue
        payload = c.get("json") or {}
        recipient = payload.get("recipient") or {}
        if comment_id is None or recipient.get("comment_id") == comment_id:
            out.append(c)
    return out


# --- the infinite-loop test (written first) ---------------------------------


def test_the_shop_s_own_comment_is_never_answered(client, comments_on, fake_graph):
    """The worst available failure: the bot replying to itself, forever,
    publicly, on a live post."""
    body = comment_body("شكراً للكل", commenter=IG_ID)
    assert post_comment(client, body).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    with SessionLocal() as db:
        assert db.query(InstagramCommentReply).count() == 0


# --- the flag ---------------------------------------------------------------


def test_comments_disabled_means_nothing_happens_at_all(client, comments_on, fake_graph, monkeypatch):
    # The default configuration ships OFF; prove the flag gates everything by
    # flipping it back off after the fixtures enabled it.
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(comments_on, instagram_comments_enabled=False),
    )
    assert post_comment(client, comment_body()).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    with SessionLocal() as db:
        assert db.query(InstagramCommentReply).count() == 0


# --- what survives gets two actions ------------------------------------------


def test_a_valid_comment_gets_one_public_and_one_private_reply_and_a_seeded_session(
    client, comments_on, fake_graph
):
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    pubs = public_replies(fake_graph)
    privs = private_replies(fake_graph)
    assert len(pubs) == 1
    assert len(privs) == 1
    # The public ack is one of the fixed lines -- never a model call -- and
    # deterministic per comment id.
    assert pubs[0]["json"]["message"] in adapter.PUBLIC_ACKS

    # The private reply went to the messages endpoint under the comment id...
    payload = privs[0]["json"]
    assert payload["recipient"] == {"comment_id": COMMENT_ID}
    assert "بكام ده؟" in payload["message"]["text"]

    # ...and the DM thread is seeded so the next message has context.
    with session_scope() as session:
        row = session.get(SessionRow, ("instagram_dm", COMMENTER))
        assert row is not None
        history = row.history
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert MEDIA_ID in history[0]["content"]
        assert history[1]["role"] == "assistant"
        assert history[1]["content"].startswith("شفت كومنتك")


def test_the_same_comment_delivered_twice_still_gets_one_of_each(client, comments_on, fake_graph):
    body = comment_body()
    post_comment(client, body)
    post_comment(client, body)

    assert len(public_replies(fake_graph)) == 1
    assert len(private_replies(fake_graph)) == 1


def test_an_already_replied_comment_row_blocks_a_second_private_reply(
    client, comments_on, fake_graph
):
    with session_scope() as session:
        session.add(
            InstagramCommentReply(
                comment_id=COMMENT_ID,
                commenter_igsid=COMMENTER,
                public_replied=True,
                private_replied=True,
            )
        )

    assert post_comment(client, comment_body()).status_code == 200
    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []


def test_the_client_itself_refuses_a_second_completed_private_reply(
    comments_on, fake_graph, seeded
):
    from integrations.instagram.client import InstagramClient

    with session_scope() as session:
        session.add(
            InstagramCommentReply(
                comment_id=COMMENT_ID, commenter_igsid=COMMENTER, private_replied=True
            )
        )

    # Called only after the row has committed -- the state any retry would
    # actually encounter.
    result = InstagramClient().send_private_reply(COMMENT_ID, "أي حاجة")

    assert result.delivered is False
    assert result.error == "already_replied"
    assert fake_graph.posts == []


# --- the rest of the filter chain --------------------------------------------


def test_an_emoji_only_comment_is_dropped(client, comments_on, fake_graph):
    assert post_comment(client, comment_body("🔥🔥")).status_code == 200
    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []


def test_a_two_character_comment_is_dropped(client, comments_on, fake_graph):
    assert post_comment(client, comment_body("👍👍")).status_code == 200
    assert private_replies(fake_graph) == []


def test_a_threaded_reply_is_dropped(client, comments_on, fake_graph):
    assert post_comment(
        client, comment_body(parent_id="17900000000000099")
    ).status_code == 200
    assert private_replies(fake_graph) == []


def test_a_comment_older_than_the_max_age_is_dropped(client, comments_on, fake_graph):
    old = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    assert post_comment(client, comment_body(timestamp=old)).status_code == 200
    assert private_replies(fake_graph) == []


def test_over_the_rate_limit_drops_and_raises_exactly_one_flood_alert(
    client, comments_on, fake_graph
):
    with session_scope() as session:
        for index in range(comments_on.instagram_comment_rate_limit):
            session.add(
                InstagramCommentReply(
                    comment_id=f"1790000000000{index:04d}",
                    commenter_igsid=COMMENTER,
                    created_at=utcnow() - timedelta(minutes=index + 5),
                    public_replied=True,
                    private_replied=True,
                )
            )

    fresh_id = "17900000000009999"
    assert post_comment(client, comment_body(comment_id=fresh_id)).status_code == 200

    assert private_replies(fake_graph) == []
    with session_scope() as session:
        alerts = [
            i
            for i in queues.open_items(session, QueueKind.ALERT.value)
            if i.reason == "comment_flood"
        ]
    assert len(alerts) == 1


def test_hide_comment_exists_shipped_unused(client, comments_on, fake_graph):
    """`hide_comment` ships with a test and no caller: for a future staff
    action and the abuse path, not for the agent."""
    from integrations.instagram.client import InstagramClient

    assert InstagramClient().hide_comment(COMMENT_ID) is True
    expected_url = f"{GRAPH}/{comments_on.instagram_api_version}/{COMMENT_ID}"
    hides = [c for c in fake_graph.posts if c["url"] == expected_url]
    assert len(hides) == 1
    assert hides[0]["json"] == {"hide": True}
