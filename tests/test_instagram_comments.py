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

from assistant.channels import instagram as adapter
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


# --- classification routing (positive / negative / neither) ----------------


def _push_classification(monkeypatch, category):
    from assistant.providers.fake import ScriptedProvider

    provider = ScriptedProvider()
    provider.push_classification(category)
    monkeypatch.setattr(adapter, "get_provider", lambda: provider)
    return provider


def test_a_positive_comment_gets_liked_not_dmed(client, comments_on, fake_graph, monkeypatch):
    _push_classification(monkeypatch, "positive")
    assert post_comment(client, comment_body("حلو جداً 🖤")).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    likes = [c for c in fake_graph.posts if c["url"].endswith("/likes")]
    assert len(likes) == 1
    with session_scope() as session:
        row = session.get(InstagramCommentReply, COMMENT_ID)
        assert row is not None and row.public_replied is True


def test_a_negative_comment_gets_a_silent_alert_not_a_public_reply(
    client, comments_on, fake_graph, monkeypatch
):
    _push_classification(monkeypatch, "negative")
    assert post_comment(client, comment_body("الخدمة وحشة قوي")).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    likes = [c for c in fake_graph.posts if c["url"].endswith("/likes")]
    assert likes == []
    with session_scope() as session:
        alerts = [
            i
            for i in queues.open_items(session, QueueKind.ALERT.value)
            if i.reason == "negative_comment"
        ]
    assert len(alerts) == 1
    assert alerts[0].payload["comment_id"] == COMMENT_ID


def test_a_neither_comment_gets_no_action_at_all(client, comments_on, fake_graph, monkeypatch):
    """A bare @mention pointing a friend at the post -- the tagger is not
    asking anything themselves."""
    _push_classification(monkeypatch, "neither")
    assert post_comment(client, comment_body("@sara شوفي دي")).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    assert [c for c in fake_graph.posts if c["url"].endswith("/likes")] == []
    with session_scope() as session:
        assert queues.open_items(session, QueueKind.ALERT.value) == []


def test_an_important_comment_still_gets_the_dm_handoff(
    client, comments_on, fake_graph, monkeypatch
):
    _push_classification(monkeypatch, "important")
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    assert len(public_replies(fake_graph)) == 1
    assert len(private_replies(fake_graph)) == 1


def test_classification_unsupported_falls_back_to_important(
    client, comments_on, fake_graph, monkeypatch
):
    """RehearsalProvider (no LLM key) cannot classify -- must not silently
    drop every comment, since that is a worse outcome than the classifier
    never having existed."""
    from assistant.providers.fake import RehearsalProvider

    monkeypatch.setattr(adapter, "get_provider", RehearsalProvider)
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    assert len(public_replies(fake_graph)) == 1
    assert len(private_replies(fake_graph)) == 1


# --- post context (caption fetched fresh, never cached) ---------------------


def test_the_seeded_session_carries_the_post_s_caption(client, comments_on, fake_graph):
    fake_graph.get_json_body = {"caption": "الهودي الزيتي الجديد وصل 🖤 #wanas"}
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    with session_scope() as session:
        row = session.get(SessionRow, ("instagram_dm", COMMENTER))
        seeded = row.history[0]["content"]
    assert "الهودي الزيتي الجديد وصل" in seeded
    # The fetch itself hit the media id, not a stored/cached lookup.
    media_gets = [c for c in fake_graph.calls if c["method"] == "GET" and c["url"].endswith(f"/{MEDIA_ID}")]
    assert len(media_gets) == 1


def test_a_deleted_or_unreadable_post_seeds_the_session_with_no_note(
    client, comments_on, fake_graph
):
    """get_media failing (deleted post, private, API error) must not break
    the ack/DM flow -- it only means no caption context is added."""
    fake_graph.fail_next_posts = 0  # posts (ack + private reply) still succeed
    fake_graph.download_status = 404  # the GET for the media fails
    fake_graph.get_json_body = {}

    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200
    assert len(public_replies(fake_graph)) == 1
    assert len(private_replies(fake_graph)) == 1

    with session_scope() as session:
        row = session.get(SessionRow, ("instagram_dm", COMMENTER))
        seeded = row.history[0]["content"]
    assert seeded == f"[كومنت على بوست {MEDIA_ID}] بكام ده؟"


def test_a_post_with_no_caption_seeds_the_session_with_no_note(client, comments_on, fake_graph):
    fake_graph.get_json_body = {"caption": ""}
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    with session_scope() as session:
        row = session.get(SessionRow, ("instagram_dm", COMMENTER))
        seeded = row.history[0]["content"]
    assert seeded == f"[كومنت على بوست {MEDIA_ID}] بكام ده؟"


def test_hide_comment_exists_shipped_unused(client, comments_on, fake_graph):
    """`hide_comment` ships with a test and no caller: for a future staff
    action and the abuse path, not for the agent."""
    from integrations.instagram.client import InstagramClient

    assert InstagramClient().hide_comment(COMMENT_ID) is True
    expected_url = f"{GRAPH}/{comments_on.instagram_api_version}/{COMMENT_ID}"
    hides = [c for c in fake_graph.posts if c["url"] == expected_url]
    assert len(hides) == 1
    assert hides[0]["json"] == {"hide": True}


def test_like_comment_posts_to_the_likes_endpoint(client, comments_on, fake_graph):
    from integrations.instagram.client import InstagramClient

    assert InstagramClient().like_comment(COMMENT_ID) is True
    expected_url = f"{GRAPH}/{comments_on.instagram_api_version}/{COMMENT_ID}/likes"
    likes = [c for c in fake_graph.posts if c["url"] == expected_url]
    assert len(likes) == 1


def test_like_comment_failure_is_logged_not_raised(client, comments_on, fake_graph, caplog):
    from integrations.instagram.client import InstagramClient

    fake_graph.fail_next_posts = 1
    assert InstagramClient().like_comment(COMMENT_ID) is False
    assert "could not like comment" in caplog.text
