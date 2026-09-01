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



@pytest.fixture()
def classifier(monkeypatch):
    """Pin what the classifier says, for a test that wants a category.

    There is no default any more: an unavailable classifier is silence plus a
    `classifier_unavailable` alert, so a test that wants the DM handoff has to
    ask for it. `.category` is re-readable, unlike a scripted queue -- several
    of these post the same comment twice.
    """
    from assistant.providers.base import CommentClassification, LLMProvider

    class _Fixed(LLMProvider):
        name = "fixed-for-tests"

        def __init__(self):
            self.category = "important"
            self.calls: list[str] = []

        def classify_comment(self, text):
            self.calls.append(text)
            return CommentClassification(category=self.category)

    fixed = _Fixed()
    monkeypatch.setattr(adapter, "get_provider", lambda: fixed)
    return fixed


def alerts_named(reason):
    with session_scope() as session:
        return [
            i
            for i in queues.open_items(session, QueueKind.ALERT.value)
            if i.reason == reason
        ]


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
    client, comments_on, fake_graph, classifier
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


def test_the_same_comment_delivered_twice_still_gets_one_of_each(
    client, comments_on, fake_graph, classifier
):
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


def test_an_emoji_only_comment_is_dropped_and_costs_nothing(
    client, comments_on, fake_graph, classifier
):
    """It receives nothing, so it must not spend one of the commenter's three
    hourly slots on the way to receiving it: no reply row, and the real
    question that follows still gets its DM."""
    assert post_comment(client, comment_body("🔥🔥")).status_code == 200
    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    with SessionLocal() as db:
        assert db.query(InstagramCommentReply).count() == 0

    for index in range(comments_on.instagram_comment_rate_limit):
        post_comment(client, comment_body("🔥🔥", comment_id=f"emoji-{index}"))
    assert post_comment(
        client, comment_body("بكام ده؟", comment_id="after-the-emoji")
    ).status_code == 200
    assert len(private_replies(fake_graph)) == 1


def test_a_two_character_comment_is_dropped(client, comments_on, fake_graph):
    assert post_comment(client, comment_body("👍👍")).status_code == 200
    assert private_replies(fake_graph) == []


def test_a_reply_in_someone_else_s_thread_is_dropped(client, comments_on, fake_graph):
    """A conversation between two other people is none of the shop's business."""
    assert post_comment(
        client, comment_body(parent_id="17900000000000099")
    ).status_code == 200
    assert private_replies(fake_graph) == []
    assert public_replies(fake_graph) == []


def test_a_reply_under_our_own_reply_is_processed(client, comments_on, fake_graph, classifier):
    """Dropping every `parent_id` was right while the only public output was
    "شوف الدايركت", which nobody answers. Now that a fixed FAQ answer goes out
    in public, a follow-up under it is both likely and reasonable."""
    parent = "17900000000000077"
    with session_scope() as session:
        session.add(
            InstagramCommentReply(
                comment_id=parent,
                media_id=MEDIA_ID,
                commenter_igsid=COMMENTER,
                faq_key="shipping_cost",
                public_replied=True,
            )
        )

    body = comment_body("طب الهودي ده بكام؟", comment_id="17900000000000078", parent_id=parent)
    assert post_comment(client, body).status_code == 200
    assert len(private_replies(fake_graph)) == 1


def test_the_shop_s_own_reply_is_still_dropped_inside_our_own_thread(
    client, comments_on, fake_graph
):
    """The own-account check runs before the thread rule, so widening the
    thread rule cannot open a self-reply loop."""
    parent = "17900000000000077"
    with session_scope() as session:
        session.add(
            InstagramCommentReply(comment_id=parent, commenter_igsid=COMMENTER)
        )

    body = comment_body(
        "شكراً للكل", comment_id="17900000000000079", commenter=IG_ID, parent_id=parent
    )
    assert post_comment(client, body).status_code == 200
    assert public_replies(fake_graph) == []
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


def test_a_complaint_gets_a_public_line_a_dm_and_a_priority_alert(
    client, comments_on, fake_graph, monkeypatch
):
    """Split out of `negative` because the two deserve opposite treatment: a
    hater ignored is fine, a paying customer ignored in public is the worst
    outcome available on this surface."""
    _push_classification(monkeypatch, "complaint")
    assert post_comment(client, comment_body("بقالي أسبوع مستلمتش الأوردر")).status_code == 200

    pubs = public_replies(fake_graph)
    assert len(pubs) == 1
    # Fixed wording, and it admits nothing: nobody has looked at the order yet.
    assert pubs[0]["json"]["message"] == adapter.COMPLAINT_ACK
    assert len(private_replies(fake_graph)) == 1

    alerts = alerts_named("customer_complaint")
    assert len(alerts) == 1
    assert alerts[0].payload["priority"] == "high"
    assert "مستلمتش" in alerts[0].payload["text"]


def test_a_complaint_says_nothing_in_public_when_public_replies_are_off(
    client, comments_on, fake_graph, monkeypatch
):
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(comments_on, instagram_public_reply_enabled=False),
    )
    _push_classification(monkeypatch, "complaint")
    assert post_comment(client, comment_body("وصلني مقاس غلط")).status_code == 200

    assert public_replies(fake_graph) == []
    assert len(private_replies(fake_graph)) == 1
    assert len(alerts_named("customer_complaint")) == 1


def test_spam_gets_an_alert_and_is_never_hidden(
    client, comments_on, fake_graph, monkeypatch
):
    """`hide_comment` stays uncalled deliberately: hiding is invisible to the
    shop, so a misclassified real customer would vanish with no trace anyone
    could follow. The owner hides by hand, from this alert."""
    _push_classification(monkeypatch, "spam")
    assert post_comment(client, comment_body("follow me 4 free followers bit.ly/x")).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    assert [c for c in fake_graph.posts if c["url"].endswith("/likes")] == []
    hides = [c for c in fake_graph.posts if (c.get("json") or {}).get("hide")]
    assert hides == []

    alerts = alerts_named("spam_comment")
    assert len(alerts) == 1
    assert alerts[0].payload["comment_id"] == COMMENT_ID


def test_classification_unavailable_is_silence_plus_an_alert(
    client, comments_on, fake_graph, monkeypatch
):
    """A provider that cannot classify (no key, an outage, a 429) used to make
    every comment `important` -- which turned an outage into a burst of public
    replies and DMs on a live post that no model had decided on. Silence is
    the safe failure on a public surface; the alert is what stops silence
    from meaning loss."""
    from assistant.providers.fake import RehearsalProvider

    monkeypatch.setattr(adapter, "get_provider", RehearsalProvider)
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    alerts = alerts_named("classifier_unavailable")
    assert len(alerts) == 1
    assert alerts[0].payload["comment_id"] == COMMENT_ID
    assert alerts[0].payload["media_id"] == MEDIA_ID
    assert alerts[0].payload["commenter_id"] == COMMENTER
    assert alerts[0].payload["text"] == "بكام ده؟"


def test_a_provider_error_from_the_classifier_is_silence_plus_an_alert(
    client, comments_on, fake_graph, monkeypatch
):
    """The other branch: a provider that declares support and then fails."""
    from assistant.providers.base import LLMProvider, ProviderError

    class _Down(LLMProvider):
        def classify_comment(self, text):
            raise ProviderError("openrouter is having a day", kind="rate_limit")

    monkeypatch.setattr(adapter, "get_provider", _Down)
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    assert len(alerts_named("classifier_unavailable")) == 1


def test_a_classifier_crash_is_silence_plus_an_alert(
    client, comments_on, fake_graph, monkeypatch
):
    """Not a ProviderError -- anything at all. The bare except branch used to
    argue for `important` too."""
    from assistant.providers.base import LLMProvider

    class _Broken(LLMProvider):
        def classify_comment(self, text):
            raise RuntimeError("boom")

    monkeypatch.setattr(adapter, "get_provider", _Broken)
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    assert private_replies(fake_graph) == []
    assert len(alerts_named("classifier_unavailable")) == 1


# --- post context (caption fetched fresh, never cached) ---------------------


def test_the_seeded_session_carries_the_post_s_caption(
    client, comments_on, fake_graph, classifier
):
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
    client, comments_on, fake_graph, classifier
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


def test_a_post_with_no_caption_seeds_the_session_with_no_note(
    client, comments_on, fake_graph, classifier
):
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


# --- the fixed FAQ answers (no model call, no DM) ---------------------------


FAQ_EXPECTED = {
    "delivery_time": "التوصيل بياخد لغاية 4 أيام لكل محافظات مصر 🖤",
    "shipping_cost": "الشحن 110 جنيه لكل محافظات مصر 🖤",
    "payment": "بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع 🖤",
}


@pytest.mark.parametrize(
    ("key", "comment"),
    [
        ("delivery_time", "التوصيل بياخد قد إيه؟"),
        ("delivery_time", "el delivery byakhod ad eh?"),
        ("delivery_time", "الشحن كام يوم؟"),
        ("shipping_cost", "الشحن بكام؟"),
        ("shipping_cost", "el shipping bkam"),
        ("shipping_cost", "how much is shipping"),
        ("payment", "بتاخدوا كاش؟"),
        ("payment", "fi cash on delivery?"),
        ("payment", "الدفع إزاي؟"),
    ],
)
def test_an_faq_comment_is_answered_in_public_and_stops_there(
    client, comments_on, fake_graph, classifier, key, comment
):
    """One public sentence answers it completely, for everyone scrolling past
    as well -- so no DM, no seeded session, and the classifier is never
    called, which is the saving."""
    assert post_comment(client, comment_body(comment)).status_code == 200

    pubs = public_replies(fake_graph)
    assert len(pubs) == 1
    assert pubs[0]["json"]["message"] == FAQ_EXPECTED[key]

    assert private_replies(fake_graph) == []
    assert classifier.calls == []
    with session_scope() as session:
        assert session.get(SessionRow, ("instagram_dm", COMMENTER)) is None


def test_a_product_question_is_not_an_faq(client, comments_on, fake_graph, classifier):
    """"الهودي ده بكام؟" is a price the shop has to look up, not the flat
    shipping rate -- answering it from a table would publish a wrong number
    under a post."""
    assert post_comment(client, comment_body("الهودي الأسود ده بكام؟")).status_code == 200

    assert classifier.calls == ["الهودي الأسود ده بكام؟"]
    assert len(private_replies(fake_graph)) == 1
    pubs = public_replies(fake_graph)
    assert pubs[0]["json"]["message"] in adapter.PUBLIC_ACKS


def test_an_faq_reply_writes_its_row_and_a_redelivery_sends_nothing_twice(
    client, comments_on, fake_graph
):
    """The row goes in before the send, exactly as the DM path does it: a
    crash between write and send, or a duplicate webhook delivery, must never
    put the same reply under a customer's comment twice."""
    body = comment_body("الشحن بكام؟")
    assert post_comment(client, body).status_code == 200
    assert post_comment(client, body).status_code == 200

    assert len(public_replies(fake_graph)) == 1
    with session_scope() as session:
        row = session.get(InstagramCommentReply, COMMENT_ID)
        assert row is not None
        assert row.faq_key == "shipping_cost"
        assert row.public_replied is True
        assert row.private_replied is False


def test_faq_replies_do_not_spend_the_dm_budget(
    client, comments_on, fake_graph, classifier
):
    """The 3/hour cap exists to stop a flood of *DMs*. An FAQ reply sends no
    DM and costs no model call, so it must not consume that budget."""
    for index in range(comments_on.instagram_faq_rate_limit):
        assert post_comment(
            client, comment_body("الشحن بكام؟", comment_id=f"faq-{index}")
        ).status_code == 200
    assert len(public_replies(fake_graph)) == comments_on.instagram_faq_rate_limit

    assert post_comment(
        client, comment_body("الهودي ده بكام؟", comment_id="a-real-question")
    ).status_code == 200
    assert len(private_replies(fake_graph)) == 1


def test_the_sixth_faq_comment_in_an_hour_is_dropped(client, comments_on, fake_graph):
    """Visible under a post, so not unlimited -- but a chatty commenter is not
    a flood, so it drops quietly with no alert."""
    for index in range(comments_on.instagram_faq_rate_limit):
        post_comment(client, comment_body("الشحن بكام؟", comment_id=f"faq-{index}"))

    assert post_comment(
        client, comment_body("الشحن بكام؟", comment_id="one-too-many")
    ).status_code == 200

    replied_to = [c["url"].rsplit("/", 2)[-2] for c in public_replies(fake_graph)]
    assert "one-too-many" not in replied_to
    assert len(replied_to) == comments_on.instagram_faq_rate_limit
    # Not a flood: nothing reached a person's inbox and nothing cost a model
    # call, so there is nothing here for staff to look at.
    assert alerts_named("comment_flood") == []


def test_public_replies_off_turns_an_faq_into_a_dm_handoff(
    client, comments_on, fake_graph, classifier, monkeypatch
):
    """The flag means "do not speak in public", not "ignore the customer": the
    question still deserves an answer, so it takes the `important` path."""
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(comments_on, instagram_public_reply_enabled=False),
    )
    assert post_comment(client, comment_body("الشحن بكام؟")).status_code == 200

    assert public_replies(fake_graph) == []
    assert len(private_replies(fake_graph)) == 1
    assert classifier.calls == ["الشحن بكام؟"]


def test_the_faq_table_is_a_lookup_not_a_classifier():
    """No model call is reachable from here -- that is the whole point of the
    module: the public surface never displays a sentence a model chose."""
    from assistant import comment_faq

    assert comment_faq.match("") is None
    assert comment_faq.match("🔥") is None
    assert comment_faq.match("عايز هودي أسود") is None
    assert set(comment_faq.FAQ_REPLIES) == set(FAQ_EXPECTED)
    for key, reply in FAQ_EXPECTED.items():
        assert comment_faq.reply_for(key) == reply
    # No link in the payment answer: Instagram suppresses the reach of a
    # comment carrying one, so the answer would be published and then unread.
    assert "http" not in comment_faq.FAQ_REPLIES["payment"]
