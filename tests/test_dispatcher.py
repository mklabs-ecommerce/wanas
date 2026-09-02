"""Buffering inbound messages instead of answering the webhook with them.

Two things are being protected here, and they fail in ways that are hard to
see from a log: three fragments of one sentence costing three model calls, and
two fragments of one conversation being answered by two threads at once.
"""

from __future__ import annotations

import threading
import time

from assistant.dispatcher import MessageDispatcher, Pending


def test_fragments_of_one_sentence_become_one_turn():
    handled: list[tuple[str, str]] = []
    dispatcher = MessageDispatcher(
        lambda key, item: handled.append((key, item.text)), debounce_seconds=0.2, max_workers=2
    )
    try:
        for fragment in ("عايز هودي", "أسود", "لارج"):
            dispatcher.submit("2010", Pending(texts=[fragment]))
            time.sleep(0.02)
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    # One turn, not three -- and the fragments arrive in the order they were sent.
    assert handled == [("2010", "عايز هودي\nأسود\nلارج")]


def test_different_conversations_are_not_merged():
    handled: dict[str, str] = {}
    dispatcher = MessageDispatcher(
        lambda key, item: handled.__setitem__(key, item.text), debounce_seconds=0.1
    )
    try:
        dispatcher.submit("2010", Pending(texts=["أنا الأول"]))
        dispatcher.submit("2020", Pending(texts=["وأنا التاني"]))
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    assert handled == {"2010": "أنا الأول", "2020": "وأنا التاني"}


def test_media_paths_survive_the_merge():
    handled: list[Pending] = []
    dispatcher = MessageDispatcher(
        lambda key, item: handled.append(item), debounce_seconds=0.15
    )
    try:
        dispatcher.submit("2010", Pending(texts=["شوف ده"], image_paths=["a.jpg"]))
        dispatcher.submit("2010", Pending(audio_paths=["b.ogg"], last_message_id="wamid.2"))
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    assert handled[0].image_paths == ["a.jpg"]
    assert handled[0].audio_paths == ["b.ogg"]
    # The newest id wins: it is the one whose ticks should turn blue.
    assert handled[0].last_message_id == "wamid.2"


def test_reply_to_and_per_item_ids_survive_the_merge():
    """A reply-to id captured on one fragment must still be resolvable
    against an image/audio id that arrived in an earlier fragment of the
    same debounced batch -- see assistant/channels/whatsapp.py's
    `_annotate_replies`."""
    handled: list[Pending] = []
    dispatcher = MessageDispatcher(lambda key, item: handled.append(item), debounce_seconds=0.15)
    try:
        dispatcher.submit("2010", Pending(image_paths=["a.jpg"], image_ids=["wamid.img1"]))
        dispatcher.submit(
            "2010",
            Pending(
                texts=["مقاس M لو سمحت"],
                text_ids=["wamid.txt1"],
                reply_to={"wamid.txt1": "wamid.img1"},
            ),
        )
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    assert handled[0].image_ids == ["wamid.img1"]
    assert handled[0].text_ids == ["wamid.txt1"]
    assert handled[0].reply_to == {"wamid.txt1": "wamid.img1"}


def test_one_conversation_is_never_answered_twice_at_once():
    """A second turn for the same customer waits for the first to finish.

    Without the per-conversation lock, two turns race over the same session
    row and one of them silently overwrites the other's history.
    """
    overlapping = []
    active = set()
    guard = threading.Lock()

    def slow(key, item):
        with guard:
            overlapping.append(key in active)
            active.add(key)
        time.sleep(0.1)
        with guard:
            active.discard(key)

    dispatcher = MessageDispatcher(slow, debounce_seconds=0.05, max_workers=4)
    try:
        dispatcher.submit("2010", Pending(texts=["one"]))
        time.sleep(0.12)  # long enough for the first to be released and running
        dispatcher.submit("2010", Pending(texts=["two"]))
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    assert overlapping == [False, False]


def test_zero_debounce_runs_inline():
    """What the test suite uses, and what production must never use."""
    handled = []
    dispatcher = MessageDispatcher(lambda key, item: handled.append(item.text), debounce_seconds=0)
    try:
        dispatcher.submit("2010", Pending(texts=["now"]))
        # No waiting: it already ran, in this thread.
        assert handled == ["now"]
    finally:
        dispatcher.shutdown()


def test_a_failing_handler_does_not_kill_the_worker():
    """One bad message must not silently drop every later one."""
    handled = []

    def sometimes(key, item):
        if item.text == "boom":
            raise RuntimeError("bad message")
        handled.append(item.text)

    dispatcher = MessageDispatcher(sometimes, debounce_seconds=0.05)
    try:
        dispatcher.submit("2010", Pending(texts=["boom"]))
        assert dispatcher.wait_idle(5)
        dispatcher.submit("2020", Pending(texts=["fine"]))
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    assert handled == ["fine"]


def test_the_dispatcher_returns_before_the_work_is_done():
    """The whole point: the webhook answers 200 without waiting for a model."""
    started = threading.Event()
    release = threading.Event()

    def slow(key, item):
        started.set()
        release.wait(5)

    dispatcher = MessageDispatcher(slow, debounce_seconds=0.05)
    try:
        began = time.monotonic()
        dispatcher.submit("2010", Pending(texts=["hello"]))
        assert time.monotonic() - began < 0.05
        assert started.wait(5)
        assert not dispatcher._idle.is_set()
        release.set()
        assert dispatcher.wait_idle(5)
    finally:
        release.set()
        dispatcher.shutdown()


def test_shutdown_answers_what_is_still_buffered():
    """A deploy must not throw away a customer's half-typed sentence.

    `app.py`'s lifespan calls `shutdown(wait=True)` precisely so buffered
    fragments get their turn. It did not: cancelling the timers left the
    `Pending` sitting in `_pending`, and `ThreadPoolExecutor.shutdown` only
    waits for tasks already submitted. The customer had been recorded as
    having written (`record_inbound`) and was then never answered -- which the
    dashboard shows as unanswered forever, with nothing in the log.
    """
    handled: list[tuple[str, str]] = []
    dispatcher = MessageDispatcher(
        lambda key, item: handled.append((key, item.text)),
        # Long enough that nothing can fire on its own before shutdown.
        debounce_seconds=30,
        max_workers=2,
    )
    dispatcher.submit("2010", Pending(texts=["عايز هودي"]))
    dispatcher.submit("2020", Pending(texts=["بكام؟"]))
    assert handled == []  # still inside the debounce window

    dispatcher.shutdown(wait=True)

    assert sorted(handled) == [("2010", "عايز هودي"), ("2020", "بكام؟")]


def test_conversation_locks_do_not_accumulate():
    """One `threading.Lock` per customer who ever messaged, kept for the life
    of the process, is an unbounded leak on a long-lived instance. The lock
    still has to outlive any thread waiting on it, which is why it is
    refcounted rather than deleted after each run."""
    dispatcher = MessageDispatcher(lambda key, item: None, debounce_seconds=0)
    try:
        for i in range(50):
            dispatcher.submit(f"customer-{i}", Pending(texts=["هاي"]))
        assert dispatcher.wait_idle(5)
        assert dispatcher._conversation_locks == {}
        assert dispatcher._lock_users == {}
    finally:
        dispatcher.shutdown()


def test_one_conversation_is_still_answered_one_turn_at_a_time():
    """The refcounting above must not weaken the guarantee it is built around:
    two fragments of one conversation never run concurrently."""
    overlaps: list[int] = []
    running = 0
    guard = threading.Lock()

    def handler(key, item):
        nonlocal running
        with guard:
            running += 1
            overlaps.append(running)
        time.sleep(0.05)
        with guard:
            running -= 1

    dispatcher = MessageDispatcher(handler, debounce_seconds=0.05, max_workers=4)
    try:
        for _ in range(6):
            dispatcher.submit("2010", Pending(texts=["هاي"]))
            time.sleep(0.08)
        assert dispatcher.wait_idle(10)
    finally:
        dispatcher.shutdown()

    assert max(overlaps) == 1
