"""Stored history -> bubbles a person can read (assistant/display.py), shared
by the harness and the staff dashboard.
"""

from __future__ import annotations

from datetime import datetime

from assistant import messages as msg
from assistant.display import display_history


def test_a_plain_user_message_has_no_media():
    items = display_history([msg.user("عايز هودي")])
    assert len(items) == 1
    assert {k: v for k, v in items[0].items() if k != "at"} == {
        "kind": "user",
        "text": "عايز هودي",
        "images": [],
        "audio": [],
    }
    # ...and it carries its own time, which is what the dashboard shows next
    # to it. Asserted as "present and parseable", never as an exact value.
    assert datetime.fromisoformat(items[0]["at"]).tzinfo is not None


def test_a_customer_message_never_carries_a_seen_state():
    """"Seen" is about what the *customer* has read, so it exists only for
    what the shop sent them. A tick on their own message would be the
    dashboard claiming to know something it cannot: whether the shop has read
    it is not a question WhatsApp answers, and staff reading the thread are
    the answer anyway."""
    items = display_history([msg.user("عايز هودي")])
    assert "receipt" not in items[0]
    assert "seen_at" not in items[0]

    # The outbound side of the same transcript is where it does belong.
    outbound = display_history([msg.assistant("أهلاً بيك")])
    assert "receipt" in outbound[0]


def test_a_voice_notes_audio_survives_alongside_its_transcript():
    """The transcript is not a replacement for the audio -- both must reach
    the dashboard, never one instead of the other."""
    items = display_history([msg.user("عايز الهودي الزيتي", audio=["data/inbound/a.ogg"])])
    assert items[0]["text"] == "عايز الهودي الزيتي"
    assert items[0]["audio"] == ["data/inbound/a.ogg"]


def test_a_photos_path_survives_alongside_its_description():
    items = display_history([msg.user("[الزبون بعت صورة]", images=["data/inbound/b.jpg"])])
    assert items[0]["images"] == ["data/inbound/b.jpg"]


def test_a_staff_reply_is_still_labelled_by():
    items = display_history([msg.assistant("هبعتلك التفاصيل", by="staff")])
    assert items[0]["by"] == "staff"
