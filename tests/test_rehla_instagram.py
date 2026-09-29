"""Rehla on Instagram comments: the post's caption names the product, and a
short comment is greeted instead of dropped."""

from __future__ import annotations

from domain.db import session_scope
from domain.models import InstagramCommentReply, SessionRow
from tests.test_instagram_comments import (  # noqa: F401  (fixtures)
    COMMENT_ID,
    COMMENTER,
    IG_ID,
    classifier,
    client,
    comment_body,
    comments_on,
    fake_graph,
    post_comment,
    private_replies,
    public_replies,
)


def _dm_text(fake_graph):
    sent = private_replies(fake_graph, COMMENT_ID)
    assert len(sent) == 1
    text = sent[0]["json"]["message"]["text"]
    # Direction marks are added at the send boundary (common/bidi.py).
    return "".join(ch for ch in text if ch not in "‎‏⁦⁧⁨⁩")


def test_a_price_comment_on_a_known_post_is_answered_with_the_price(
    client, comments_on, fake_graph, classifier
):
    classifier.category = "price"
    fake_graph.get_json_body = {"caption": "Rehla White T-Shirt 🤍 متاح دلوقتي", "permalink": "p"}
    assert post_comment(client, comment_body("بكام")).status_code == 200

    text = _dm_text(fake_graph)
    assert "Rehla White T-Shirt" in text
    assert "600" in text and "750" in text          # price, and the pre-sale price
    assert "المقاسات المتاحة" in text
    assert "أنهي قطعة" not in text                    # never "which product?"

    with session_scope() as db:
        history = db.get(SessionRow, ("instagram_dm", COMMENTER)).history
    from assistant.tools.base import last_product

    assert last_product(history)["product_id"] == "t-shirt-white-shirt"


def test_an_ambiguous_caption_keeps_the_old_opener(client, comments_on, fake_graph, classifier):
    classifier.category = "price"
    fake_graph.get_json_body = {"caption": "تيشيرتات رحلة الجديدة"}
    assert post_comment(client, comment_body("بكام")).status_code == 200
    assert "بكام" in _dm_text(fake_graph)             # the category opener quotes the comment


def test_a_short_comment_gets_a_warm_line_and_a_greeting(client, comments_on, fake_graph, classifier):
    fake_graph.get_json_body = {"caption": ""}
    assert post_comment(client, comment_body("Hm")).status_code == 200

    public = public_replies(fake_graph)
    assert len(public) == 1 and "🤍" in public[0]["json"]["message"]
    assert _dm_text(fake_graph) == "أهلًا بيكي في رحلة 🤍 تحبي أساعدك في إيه؟"
    assert classifier.calls == []                      # no model call on "Hm"


def test_a_short_comment_on_a_known_post_shows_the_product(client, comments_on, fake_graph, classifier):
    fake_graph.get_json_body = {"caption": "Rehla Yoga Pants"}
    assert post_comment(client, comment_body("🔥")).status_code == 200
    text = _dm_text(fake_graph)
    assert text.startswith("أهلًا بيكي في رحلة 🤍") and "Rehla Yoga Pants" in text and "650" in text


def test_short_comments_still_one_reply_and_never_our_own(client, comments_on, fake_graph, classifier):
    fake_graph.get_json_body = {"caption": ""}
    post_comment(client, comment_body("."))
    post_comment(client, comment_body("."))            # Meta redelivery
    assert len(public_replies(fake_graph)) == 1
    assert len(private_replies(fake_graph, COMMENT_ID)) == 1

    post_comment(client, comment_body("Hm", comment_id="1790000000000009", commenter=IG_ID))
    with session_scope() as db:
        assert db.get(InstagramCommentReply, "1790000000000009") is None
