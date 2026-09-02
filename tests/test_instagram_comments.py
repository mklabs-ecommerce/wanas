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

from assistant import comment_replies
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
#: The same account's *app-scoped* id -- a different number for one account.
APP_SCOPED_ID = "28440000000000000"
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
            self.category = "price"
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


def test_the_shop_s_own_app_scoped_comment_is_never_answered(
    client, comments_on, fake_graph, monkeypatch
):
    """The same failure, wearing Instagram's *other* id for the same account.

    Instagram Login hands one account out under two numbers -- the
    professional-account id (`?fields=user_id`, what INSTAGRAM_ACCOUNT_ID
    holds) and an app-scoped id (`?fields=id`) -- and which one arrives as
    `from.id` is Meta's choice. Matching only the first let the shop's own
    comment through as a stranger's, which is the self-reply loop with an
    extra step.
    """
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(comments_on, instagram_app_scoped_id=APP_SCOPED_ID),
    )
    body = comment_body("شكراً للكل", commenter=APP_SCOPED_ID)
    assert post_comment(client, body).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []
    with SessionLocal() as db:
        assert db.query(InstagramCommentReply).count() == 0


def test_a_real_commenter_is_not_mistaken_for_the_shop(client, comments_on, fake_graph, monkeypatch):
    """The widened check must not swallow customers: an ordinary commenter
    shares neither id, so the chain runs exactly as before."""
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(comments_on, instagram_app_scoped_id=APP_SCOPED_ID),
    )
    assert post_comment(client, comment_body("الهودي ده بكام؟")).status_code == 200

    with SessionLocal() as db:
        assert db.query(InstagramCommentReply).count() == 1


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
    # The public line is one of the fixed lines in that category's bank --
    # never a model call -- and deterministic per comment id.
    assert pubs[0]["json"]["message"] == comment_replies.public_reply("price", COMMENT_ID)

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


def test_compliments_do_not_spend_the_dm_budget(client, comments_on, fake_graph, monkeypatch):
    """The bug that made a real question go unanswered after three compliments.

    The DM cap used to be charged at ingest, before anything knew whether a DM
    would be sent -- so comments that only ever get a public line (a
    compliment, an FAQ answer) burned it. Someone who wrote three nice things
    under a post and then asked what it cost got silence for the one message
    that was actually a question.
    """
    with session_scope() as session:
        for index in range(comments_on.instagram_comment_rate_limit):
            session.add(
                InstagramCommentReply(
                    comment_id=f"praise-{index}",
                    commenter_igsid=COMMENTER,
                    created_at=utcnow() - timedelta(minutes=index + 5),
                    public_replied=True,   # a public thank-you went out
                    private_replied=False,  # ...and no DM did
                )
            )

    _push_classification(monkeypatch, "important")
    assert post_comment(
        client, comment_body("الهودي ده بكام؟", comment_id="the-real-question")
    ).status_code == 200

    assert len(private_replies(fake_graph)) == 1


def test_the_dm_cap_still_holds_and_withholds_the_public_promise(
    client, comments_on, fake_graph, monkeypatch
):
    """Three DMs in an hour is still the cap -- and when it is spent the
    public "check your DMs" line is withheld too. Promising a DM that is not
    coming is worse than saying nothing."""
    with session_scope() as session:
        for index in range(comments_on.instagram_comment_rate_limit):
            session.add(
                InstagramCommentReply(
                    comment_id=f"dmed-{index}",
                    commenter_igsid=COMMENTER,
                    created_at=utcnow() - timedelta(minutes=index + 5),
                    public_replied=True,
                    private_replied=True,
                )
            )

    _push_classification(monkeypatch, "important")
    assert post_comment(
        client, comment_body("الهودي ده بكام؟", comment_id="over-the-dm-cap")
    ).status_code == 200

    assert private_replies(fake_graph) == []
    assert public_replies(fake_graph) == []


def test_over_the_rate_limit_drops_and_raises_exactly_one_flood_alert(
    client, comments_on, fake_graph
):
    """The flood guard, which is what stops a spammer costing a model call per
    comment. It is counted at INSTAGRAM_FAQ_RATE_LIMIT now rather than
    INSTAGRAM_COMMENT_RATE_LIMIT: the latter became the DM budget alone, and
    is spent at the DM, not at ingest."""
    with session_scope() as session:
        for index in range(comments_on.instagram_faq_rate_limit):
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


def test_a_positive_comment_gets_a_public_thank_you_and_no_dm(
    client, comments_on, fake_graph, monkeypatch
):
    """A compliment gets a visible answer -- and never a DM.

    It used to get a *like*, which on this integration reached nobody:
    Instagram has no API for liking a comment, so every one 400ed and the
    branch was silent. A fixed line from POSITIVE_ACKS is what the like was
    always meant to be. Still no DM, no model-written sentence, and no call
    to the likes endpoint.
    """
    _push_classification(monkeypatch, "positive")
    assert post_comment(client, comment_body("حلو جداً 🖤")).status_code == 200

    sent = public_replies(fake_graph)
    assert len(sent) == 1
    assert sent[0]["json"]["message"] == comment_replies.public_reply("positive", COMMENT_ID)
    assert private_replies(fake_graph) == []
    assert [c for c in fake_graph.posts if c["url"].endswith("/likes")] == []
    with session_scope() as session:
        row = session.get(InstagramCommentReply, COMMENT_ID)
        assert row is not None and row.public_replied is True
        assert row.private_replied is False


def test_a_positive_comment_is_silent_when_public_replies_are_off(
    client, comments_on, fake_graph, monkeypatch
):
    """INSTAGRAM_PUBLIC_REPLY_ENABLED=0 means "do not speak in public", and a
    compliment has no private half to fall through to."""
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(comments_on, instagram_public_reply_enabled=False),
    )
    _push_classification(monkeypatch, "positive")
    assert post_comment(client, comment_body("حلو جداً 🖤")).status_code == 200

    assert public_replies(fake_graph) == []
    assert private_replies(fake_graph) == []


def test_a_negative_comment_gets_a_calm_public_line_and_an_alert(
    client, comments_on, fake_graph, monkeypatch
):
    """This used to be silence plus an alert, and silence was the bug.

    A bad word under a live post is read by everyone scrolling past, and the
    shop saying nothing is what they see. One short, calm, un-defensive line
    is written for *them*, not for the person who wrote the comment. Still no
    DM: chasing a critic into their inbox is how a bad comment becomes a
    screenshot.
    """
    _push_classification(monkeypatch, "negative")
    assert post_comment(client, comment_body("الخدمة وحشة قوي")).status_code == 200

    pubs = public_replies(fake_graph)
    assert len(pubs) == 1
    assert pubs[0]["json"]["message"] == comment_replies.public_reply("negative", COMMENT_ID)
    assert private_replies(fake_graph) == []
    assert [c for c in fake_graph.posts if c["url"].endswith("/likes")] == []

    alerts = alerts_named("negative_comment")
    assert len(alerts) == 1
    assert alerts[0].payload["comment_id"] == COMMENT_ID
    # Same urgency as a complaint. The public line is the *end* of what the
    # shop says here -- no DM opens behind it -- so an alert nobody works is
    # a bad comment nobody ever read.
    assert alerts[0].payload["priority"] == "high"


def test_the_negative_line_acknowledges_and_promises_nothing(client, comments_on, fake_graph, monkeypatch):
    """It must not offer the DM, because no DM opens.

    These lines used to end in "تحت أمرك في الدايركت" -- published under a
    live post, inviting someone into a thread the shop was never going to
    open. A customer who accepted it landed in an inbox with no alert pointing
    at it; a hater who accepted it got the private argument the no-DM rule
    exists to avoid. Acknowledging is the whole job.
    """
    _push_classification(monkeypatch, "negative")
    assert post_comment(client, comment_body("أوحش محل")).status_code == 200

    published = public_replies(fake_graph)[0]["json"]["message"]
    assert "دايركت" not in published
    assert private_replies(fake_graph) == []


def test_no_negative_variant_mentions_the_dm():
    """The whole bank, not just the one line this comment id happens to pick.

    A rule enforced on one variant is a rule that holds until the next
    `crc32`.
    """
    from assistant.comment_replies import _NEGATIVE

    for line in _NEGATIVE:
        assert "دايركت" not in line, line
        assert "تحت أمرك" not in line, line


def test_tagging_a_friend_gets_a_light_public_line_and_no_dm(
    client, comments_on, fake_graph, monkeypatch
):
    """A bare @mention pointing a friend at the post.

    The tagger is not asking anything, so there is nothing to DM them about --
    but the person worth winning over is the *friend* who is about to open
    this notification, and they should not find the shop silent under its own
    post. One light line, no DM, no alert.
    """
    _push_classification(monkeypatch, "tag_friend")
    assert post_comment(client, comment_body("@sara شوفي دي")).status_code == 200

    pubs = public_replies(fake_graph)
    assert len(pubs) == 1
    assert pubs[0]["json"]["message"] == comment_replies.public_reply("tag_friend", COMMENT_ID)
    assert private_replies(fake_graph) == []
    with session_scope() as session:
        assert queues.open_items(session, QueueKind.ALERT.value) == []


def test_an_important_comment_still_gets_the_dm_handoff(
    client, comments_on, fake_graph, monkeypatch
):
    _push_classification(monkeypatch, "important")
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    # `important` is a retired name; LEGACY_COMMENT_CATEGORIES maps it onto
    # `other`, which still answers publicly and still opens the DM. A model
    # pinned to the old prompt therefore degrades to a polite answer rather
    # than to silence.
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
    assert pubs[0]["json"]["message"] == comment_replies.public_reply("complaint", COMMENT_ID)
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


def test_there_is_no_like_comment_and_nothing_calls_the_likes_endpoint(
    client, comments_on, fake_graph, monkeypatch
):
    """Instagram cannot like a comment, so nothing may try.

    `POST /{ig-comment-id}/likes` is answered 400 "does not support this
    operation" by Meta for every comment -- including ones the same token
    loads fine over GET, which is how this was told apart from a missing
    object or a missing scope. The old test asserted only that the client
    posted to that URL, which it faithfully did; the fake shelf accepts any
    endpoint, so a call Meta rejects 100% of the time passed forever.
    """
    from integrations.instagram.client import InstagramClient

    assert not hasattr(InstagramClient, "like_comment")

    _push_classification(monkeypatch, "positive")
    assert post_comment(client, comment_body("تحفة يا برو")).status_code == 200

    assert [c for c in fake_graph.posts if c["url"].endswith("/likes")] == []
    # The compliment is answered in public -- just never through /likes.
    assert len(public_replies(fake_graph)) == 1
    assert private_replies(fake_graph) == []


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
    assert pubs[0]["json"]["message"] == comment_replies.public_reply("price", COMMENT_ID)


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


# --- the redesign: every category answers somebody ---------------------------


def test_every_category_has_an_action_and_none_is_silent():
    """The property the old if/elif chain could not state.

    `negative` shipped for months classified, alerted on, and never answered,
    because a missing branch is invisible. Now the routing is a table, so
    "does every category do something?" is something a test can read.
    """
    from assistant.channels.instagram import _ACTIONS
    from assistant.providers.base import COMMENT_CATEGORIES

    assert set(_ACTIONS) == set(COMMENT_CATEGORIES)
    for category, action in _ACTIONS.items():
        assert action.public or action.dm or action.alert_reason, category
        if category == "spam":
            # The one deliberate exception: answering a scam bot in public
            # republishes it to everyone reading the post.
            assert not action.public and not action.dm
            assert action.alert_reason
        else:
            assert action.public, f"{category} has no customer-visible answer"


def test_every_category_has_reply_variants_and_they_differ():
    """A bank, not a line. One line per category is what made two customers
    asking the same question get identical text."""
    from assistant.channels.instagram import _ACTIONS

    for category, action in _ACTIONS.items():
        if not action.public:
            continue
        assert comment_replies.bank_size(category) >= 4, category
        # Every variant distinct -- a bank with a duplicate is a smaller bank.
        bank = {
            comment_replies.public_reply(category, f"seed-{i}") for i in range(400)
        }
        assert len(bank) == comment_replies.bank_size(category), category


def test_two_different_comments_get_different_wording():
    """The actual complaint: two price questions, one copy-pasted answer."""
    seen = {comment_replies.public_reply("price", f"comment-{i}") for i in range(60)}
    assert len(seen) == comment_replies.bank_size("price")

    openers = {comment_replies.dm_opener("price", f"comment-{i}", "بكام؟") for i in range(60)}
    assert len(openers) > 1


def test_variant_choice_is_stable_for_one_comment():
    """Deterministic, not random: Meta redelivers a webhook whenever it does
    not get a clean 200, and a retry must reproduce the same sentence rather
    than put a second differently-worded reply under one comment."""
    first = comment_replies.public_reply("price", COMMENT_ID)
    assert all(comment_replies.public_reply("price", COMMENT_ID) == first for _ in range(50))


def test_the_dm_opener_quotes_the_customer_and_is_category_shaped():
    opener = comment_replies.dm_opener("size", COMMENT_ID, "عندكم لارج؟")
    assert "عندكم لارج؟" in opener
    # A size opener talks about sizes; it is not the price opener with a
    # different noun swapped in.
    price = comment_replies.dm_opener("price", COMMENT_ID, "عندكم لارج؟")
    assert opener != price


@pytest.mark.parametrize(
    "category",
    ["price", "availability", "size", "variant", "product_info", "order_status", "other"],
)
def test_each_question_category_gets_a_public_line_and_a_dm(
    client, comments_on, fake_graph, monkeypatch, category
):
    _push_classification(monkeypatch, category)
    assert post_comment(client, comment_body("سؤال")).status_code == 200

    pubs = public_replies(fake_graph)
    assert len(pubs) == 1
    assert pubs[0]["json"]["message"] == comment_replies.public_reply(category, COMMENT_ID)
    assert len(private_replies(fake_graph)) == 1


@pytest.mark.parametrize("category", ["positive", "tag_friend", "negative"])
def test_each_no_dm_category_gets_a_public_line_and_no_dm(
    client, comments_on, fake_graph, monkeypatch, category
):
    _push_classification(monkeypatch, category)
    assert post_comment(client, comment_body("كلام")).status_code == 200

    pubs = public_replies(fake_graph)
    assert len(pubs) == 1
    assert pubs[0]["json"]["message"] == comment_replies.public_reply(category, COMMENT_ID)
    assert private_replies(fake_graph) == []


def test_an_order_status_question_is_answered_and_flagged(
    client, comments_on, fake_graph, monkeypatch
):
    """Neither angry nor browsing: waiting on something already paid for. It
    gets its own voice, a DM asking for the order number, and a queue item --
    it used to be swallowed by `important` and read like a sales reply."""
    _push_classification(monkeypatch, "order_status")
    assert post_comment(client, comment_body("الأوردر بتاعي فين؟")).status_code == 200

    assert len(public_replies(fake_graph)) == 1
    assert len(private_replies(fake_graph)) == 1
    assert len(alerts_named("order_status_comment")) == 1


def test_an_unknown_category_is_answered_as_other_not_dropped(
    client, comments_on, fake_graph, monkeypatch
):
    """A model that invents a category must not produce silence on a live
    post -- it degrades to the polite catch-all that asks."""
    _push_classification(monkeypatch, "something_new")
    assert post_comment(client, comment_body("سؤال غريب")).status_code == 200

    pubs = public_replies(fake_graph)
    assert len(pubs) == 1
    assert pubs[0]["json"]["message"] == comment_replies.public_reply("other", COMMENT_ID)


def test_public_replies_off_still_dms_the_question_categories(
    client, comments_on, fake_graph, monkeypatch
):
    """"Do not speak in public" is not "ignore the customer"."""
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(comments_on, instagram_public_reply_enabled=False),
    )
    _push_classification(monkeypatch, "price")
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    assert public_replies(fake_graph) == []
    assert len(private_replies(fake_graph)) == 1


def test_the_dm_half_can_be_switched_off_on_its_own(
    client, comments_on, fake_graph, monkeypatch
):
    """The mirror flag: answer in public, never cold-DM anyone."""
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(comments_on, instagram_comments_dm_enabled=False),
    )
    _push_classification(monkeypatch, "price")
    assert post_comment(client, comment_body("بكام ده؟")).status_code == 200

    assert len(public_replies(fake_graph)) == 1
    assert private_replies(fake_graph) == []


def test_no_public_line_ever_promises_a_price_it_does_not_send():
    """The broken promise this surface must never publish.

    A public line that says "I sent you the price" while the DM it refers to
    is an opener is a lie published under a post. The handoff banks invite the
    customer to the DM; they never claim the answer is already there.
    """
    for category in ("price", "availability", "size", "variant", "product_info"):
        for i in range(50):
            line = comment_replies.public_reply(category, f"c-{i}")
            assert "بعتلك السعر" not in line
            assert "بعتلك كل التفاصيل" not in line
