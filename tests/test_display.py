"""Stored history -> bubbles a person can read (chatbot/display.py), shared
by the harness and the staff dashboard.
"""

from __future__ import annotations

from chatbot import messages as msg
from chatbot.display import display_history


def test_a_plain_user_message_has_no_media():
    items = display_history([msg.user("عايز هودي")])
    assert items == [{"kind": "user", "text": "عايز هودي", "images": [], "audio": []}]


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
