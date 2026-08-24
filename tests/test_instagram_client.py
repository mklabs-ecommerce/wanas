"""The Instagram outbound client.

No Meta credentials exist in this environment, so everything here runs
against `tests/fake_instagram.py` -- a recording stand-in over the client's
own httpx, no network. The assertions are about the behaviours WhatsApp's
client does not have: the byte-cap chunking, the template refusal, and the
inert-unconfigured contract.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from backend.config import settings
from tests.fake_instagram import FakeInstagram

GRAPH_MESSAGES = "https://graph.instagram.com/v23.0/17841400000000000/messages"

CREDS = {"account_id": "17841400000000000", "access_token": "test-token"}


def make_client(**overrides):
    from backend.integrations.instagram_client import InstagramClient

    return InstagramClient(**{**CREDS, **overrides})


@pytest.fixture()
def fake(monkeypatch) -> FakeInstagram:
    return FakeInstagram().install(monkeypatch)


# --- a short Arabic message ------------------------------------------------


def test_a_short_arabic_message_is_one_post_with_the_right_shape(fake):
    client = make_client()
    result = client.send_text("1234567890", "عايز هودي")

    assert result.delivered is True
    assert fake.posts[-1]["url"] == GRAPH_MESSAGES
    assert fake.posts[-1]["headers"]["Authorization"] == "Bearer test-token"
    payload = fake.message_payloads()[-1]
    assert payload["recipient"] == {"id": "1234567890"}
    assert payload["message"]["text"] == "عايز هودي"


def test_the_messages_url_carries_the_configured_api_version(monkeypatch):
    from backend.integrations.instagram_client import GRAPH, InstagramClient

    client = InstagramClient(api_version="v19.0")
    assert client._messages_url() == f"{GRAPH}/v19.0/{settings.instagram_account_id}/messages"


# --- the ~1000-byte cap ----------------------------------------------------


def _long_arabic(target_bytes: int) -> str:
    sentence = "ده نص تجريبي طويل شوية عشان نجرب التقسيم. "
    text = sentence * (target_bytes // len(sentence.encode("utf-8")) + 1)
    while len(text.encode("utf-8")) < target_bytes:
        text += sentence
    return text.strip()


def test_a_1400_byte_arabic_message_is_chunked_under_950_bytes_in_order(fake):
    from backend.integrations.instagram_client import MAX_CHUNK_BYTES

    text = _long_arabic(1400)
    assert len(text.encode("utf-8")) >= 1400

    client = make_client()
    result = client.send_text("1234567890", text)

    sent = fake.texts()
    # More than one POST, every chunk within the cap, all delivered.
    assert len(sent) > 1
    assert all(len(c.encode("utf-8")) <= MAX_CHUNK_BYTES for c in sent)
    assert result.delivered is True
    assert result.error is None
    # Chunks arrive in order and carry the whole message.
    assert "".join("".join(sent).split()) == "".join(text.split())
    urls = [c["url"] for c in fake.posts]
    assert urls == [GRAPH_MESSAGES] * len(sent)


def test_chunks_break_at_sentence_boundaries_when_one_exists():
    from backend.integrations.instagram_client import MAX_CHUNK_BYTES, _chunks

    sentence = "جملة قصيرة معقولة هنا. "
    text = sentence * 40  # comfortably over 950 bytes of Arabic
    chunks = _chunks(text)
    assert len(chunks) > 1
    assert all(len(c.encode("utf-8")) <= MAX_CHUNK_BYTES for c in chunks)
    assert "".join("".join(chunks).split()) == "".join(text.split())


def test_a_single_unbreakable_word_still_splits_within_the_cap():
    from backend.integrations.instagram_client import MAX_CHUNK_BYTES, _chunks

    text = "ك" * 800 + "ه" * 800  # no spaces, no punctuation: hard-split territory
    chunks = _chunks(text)
    assert len(chunks) >= 2
    assert all(len(c.encode("utf-8")) <= MAX_CHUNK_BYTES for c in chunks)
    assert "".join(chunks) == text


# --- partial failure -------------------------------------------------------


def test_one_chunk_failing_fails_the_whole_message_and_names_the_error(fake):
    client = make_client()
    fake.fail_next_posts = 2  # both the first chunk and the second

    result = client.send_text("1234567890", _long_arabic(1400))

    assert result.delivered is False
    assert result.error is not None
    assert "nope" in result.error


def test_an_httpx_exception_becomes_a_logged_failure_not_a_raise(fake):
    import httpx

    client = make_client()
    # What httpx itself raises on an unreachable host -- the type `_post`
    # catches, not the builtin.
    fake.raise_next_post = httpx.ConnectError("no route to host")

    result = client.send_text("1234567890", "أهلاً")

    assert result.delivered is False
    assert "no route to host" in result.error


# --- templates -------------------------------------------------------------


def test_send_template_refuses_and_posts_nothing(fake, caplog):
    """Deliberate: Instagram has no template concept. The named error is what
    lets send_proactive fall through to its staff alert."""
    client = make_client()

    with caplog.at_level(logging.WARNING, logger="wanas.instagram"):
        result = client.send_template("1234567890", "order_confirmation")

    assert result.delivered is False
    assert result.error == "instagram_has_no_templates"
    assert fake.calls == []
    # The warning names the template so nobody later mistakes the refusal for
    # a missing configuration.
    assert any("order_confirmation" in record.message for record in caplog.records)


# --- unconfigured ----------------------------------------------------------


def test_an_unconfigured_client_logs_and_returns_not_delivered(fake, monkeypatch):
    blank = dataclasses.replace(settings, instagram_account_id="", instagram_access_token="")
    monkeypatch.setattr(
        "backend.integrations.instagram_client.settings",
        blank,
    )
    from backend.integrations.instagram_client import InstagramClient

    client = InstagramClient()
    assert client._configured is False

    result = client.send_text("1234567890", "أهلاً")

    assert result.delivered is False
    assert result.error == "instagram_not_configured"
    assert fake.calls == []  # nothing reached for the network


def test_mark_seen_and_typing_post_sender_actions(fake):
    client = make_client()

    assert client.mark_seen("1234567890") is True
    assert client.typing_on("1234567890") is True

    actions = [p["json"].get("sender_action") for p in fake.posts]
    assert actions == ["mark_seen", "typing_on"]
    assert all(p["json"]["recipient"] == {"id": "1234567890"} for p in fake.posts)


def test_mark_as_read_is_the_protocol_no_op(fake):
    client = make_client()
    assert client.mark_as_read("whatever-mid") is True
    assert fake.calls == []


# --- quick replies (STEP 8) -------------------------------------------------


def test_a_three_button_payload_becomes_three_quick_replies(fake):
    payload = {
        "kind": "buttons",
        "body": "تحب تشوف إيه؟",
        "buttons": [
            {"id": "hoodies", "title": "هوديز"},
            {"id": "tees", "title": "تيشيرتات"},
            {"id": "joggers", "title": "جوجرز"},
        ],
    }
    result = make_client().send_interactive("1234567890", payload)

    assert result.delivered is True
    message = fake.message_payloads()[-1]["message"]
    assert [q["payload"] for q in message["quick_replies"]] == ["hoodies", "tees", "joggers"]
    assert all(q["content_type"] == "text" for q in message["quick_replies"])
    assert message["text"] == "تحب تشوف إيه؟"


def test_a_12_row_list_becomes_12_quick_replies_with_ids_as_payloads(fake):
    rows = [{"id": f"gov-{i}", "title": f"محافظة {i}", "description": "وصف"} for i in range(12)]
    payload = {
        "kind": "list",
        "body": "اختار المحافظة",
        "button": "اختار",
        "sections": [{"title": "المحافظات", "rows": rows}],
    }
    make_client().send_interactive("1234567890", payload)

    quick_replies = fake.message_payloads()[-1]["message"]["quick_replies"]
    assert len(quick_replies) == 12
    assert [q["payload"] for q in quick_replies] == [f"gov-{i}" for i in range(12)]
    # Row descriptions are dropped: there is no place for them on Instagram.
    assert all("description" not in q for q in quick_replies)


def test_a_27_row_list_degrades_to_a_numbered_text_body(fake):
    """The 27-governorate picker is the >13 case. Meta would reject 27 quick
    replies; the numbered text still asks the same question, and shipping's
    `resolve` handles the free-text answer."""
    rows = [{"id": f"gov-{i}", "title": f"محافظة رقم {i} بالعربي"} for i in range(27)]
    payload = {
        "kind": "list",
        "body": "اختار المحافظة",
        "sections": [{"rows": rows}],
    }
    result = make_client().send_interactive("1234567890", payload)

    assert result.delivered is True
    messages = fake.message_payloads()
    assert all("quick_replies" not in m["message"] for m in messages)
    body = "\n".join(m["message"]["text"] for m in messages)
    assert "1. محافظة رقم 0 بالعربي" in body
    assert "27. محافظة رقم 26 بالعربي" in body
    assert "رقم الاختيار" in body


def test_an_over_long_quick_reply_title_is_truncated_by_the_client(fake):
    payload = {
        "kind": "buttons",
        "body": "اختار",
        "buttons": [{"id": "long", "title": "ده عنوان طويل جداً وفاط عشرين حرف"}],
    }
    make_client().send_interactive("1234567890", payload)

    quick_replies = fake.message_payloads()[-1]["message"]["quick_replies"]
    assert len(quick_replies[0]["title"]) <= 20
    assert quick_replies[0]["payload"] == "long"


def test_an_unknown_interactive_kind_falls_back_to_plain_text(fake):
    payload = {"kind": "something-new", "body": "هيّا نختار"}
    result = make_client().send_interactive("1234567890", payload)

    assert result.delivered is True
    sent_texts = fake.texts()
    assert sent_texts == ["هيّا نختار"]
    assert all("quick_replies" not in (m["message"]) for m in fake.message_payloads())
