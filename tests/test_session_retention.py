"""A conversation is never deleted by going quiet.

Six hours of silence used to overwrite `sessions.history` with `[]` -- inside
`load`, which the dashboard also called to *display* a conversation. A morning's
chats were gone by the afternoon, and opening one in the dashboard was itself
enough to destroy it. Expiry is now a bookmark (`context_start`), not an erase.
"""

from __future__ import annotations

from datetime import timedelta

from assistant import messages as msg, session as session_store
from config.settings import settings
from domain.models import SessionRow, utcnow

CHANNEL = "whatsapp"
WHO = "201555000111"


def _age(session, hours: float) -> None:
    row = session.get(SessionRow, (CHANNEL, WHO))
    row.updated_at = utcnow() - timedelta(hours=hours)
    session.flush()


def test_an_expired_session_keeps_every_message(seeded):
    session_store.save(seeded, CHANNEL, WHO, [msg.user("عندكم هودي؟"), msg.assistant("أيوة")])
    _age(seeded, settings.session_expiry_hours + 1)

    assert session_store.load(seeded, CHANNEL, WHO) == [], "the model starts fresh"
    kept = session_store.transcript(seeded, CHANNEL, WHO)
    assert [m["content"] for m in kept] == ["عندكم هودي؟", "أيوة"]
    assert session_store.archive_boundary(seeded, CHANNEL, WHO) == 2


def test_the_next_conversation_appends_to_the_old_one(seeded):
    session_store.save(seeded, CHANNEL, WHO, [msg.user("صباح الخير")])
    _age(seeded, settings.session_expiry_hours + 1)
    session_store.load(seeded, CHANNEL, WHO)

    session_store.append(seeded, CHANNEL, WHO, msg.user("مساء الخير"))

    assert [m["content"] for m in session_store.transcript(seeded, CHANNEL, WHO)] == [
        "صباح الخير",
        "مساء الخير",
    ]
    # The live slice is the new message alone. Compared by content rather than
    # whole-message equality: every message carries its own `at` now, so a
    # freshly built copy differs from the stored one by however long the test
    # took to get here.
    live = session_store.load(seeded, CHANNEL, WHO)
    assert [m["content"] for m in live] == ["مساء الخير"]


def test_reading_a_conversation_never_changes_it(seeded):
    session_store.save(seeded, CHANNEL, WHO, [msg.user("hello")])
    _age(seeded, settings.session_expiry_hours + 5)

    before = session_store.transcript(seeded, CHANNEL, WHO)
    assert session_store.transcript(seeded, CHANNEL, WHO) == before
    assert session_store.archive_boundary(seeded, CHANNEL, WHO) == 0, "not archived by a read"


def test_messages_past_the_history_cap_move_to_the_archive(seeded):
    history = [msg.user(f"m{n}") for n in range(settings.history_cap + 20)]
    session_store.save(seeded, CHANNEL, WHO, history)

    assert len(session_store.load(seeded, CHANNEL, WHO)) <= settings.history_cap
    assert len(session_store.transcript(seeded, CHANNEL, WHO)) == len(history)


def test_the_archive_is_bounded_so_a_row_cannot_grow_forever(seeded, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(session_store, "settings", replace(settings, session_archive_cap=10))
    for n in range(30):
        session_store.append(seeded, CHANNEL, WHO, msg.user(f"m{n}"))

    kept = session_store.transcript(seeded, CHANNEL, WHO)
    assert len(kept) == 10
    assert kept[-1]["content"] == "m29", "the oldest go, never the newest"


def test_a_staff_reset_archives_rather_than_deletes(seeded):
    session_store.save(seeded, CHANNEL, WHO, [msg.user("test order please")])

    session_store.clear(seeded, CHANNEL, WHO)

    assert session_store.load(seeded, CHANNEL, WHO) == []
    assert len(session_store.transcript(seeded, CHANNEL, WHO)) == 1


def test_purge_is_the_only_thing_that_deletes(seeded):
    session_store.save(seeded, CHANNEL, WHO, [msg.user("forget me")])

    assert session_store.purge(seeded, CHANNEL, WHO) == 1
    assert session_store.transcript(seeded, CHANNEL, WHO) == []


def test_an_old_row_with_no_bookmark_is_read_in_full(seeded):
    """Every row that existed before `context_start` did."""
    session_store.save(seeded, CHANNEL, WHO, [msg.user("a"), msg.user("b")])
    row = seeded.get(SessionRow, (CHANNEL, WHO))
    row.context_start = None  # what `ALTER TABLE` leaves on a pre-existing row
    seeded.flush()

    assert len(session_store.load(seeded, CHANNEL, WHO)) == 2
    assert len(session_store.transcript(seeded, CHANNEL, WHO)) == 2
