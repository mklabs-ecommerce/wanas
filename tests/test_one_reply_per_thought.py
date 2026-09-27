"""A text and the photo it is about get one reply, not two.

From production, WhatsApp: «عايز تيشرت ساده اوفر سايز» and a photo of one,
sent as two messages. The text started a turn after a one-second window; the
photo reached the dispatcher seconds later, after its download, and got a
turn of its own. Two replies followed, each unaware of the other's message:
the product, its photos, then the product again with other tees nobody asked
for and *their* photos.

Two things close it, both pinned here: an open batch waits for a message
still being ingested (`announce`), and a reply composed while a newer
message arrived is dropped and its batch answered together with the new one
(`has_newer` / `supersede`).
"""

from __future__ import annotations

import threading
import time

from assistant import agent
from assistant.dispatcher import MessageDispatcher, Pending
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider

CHANNEL = "whatsapp"
WHO = "201000000079"


def test_an_open_batch_waits_for_a_photo_still_downloading():
    handled: list[Pending] = []
    dispatcher = MessageDispatcher(lambda key, item: handled.append(item), debounce_seconds=0.1)
    try:
        dispatcher.announce(WHO)  # the text
        dispatcher.announce(WHO)  # the photo, still downloading
        dispatcher.settle_and_submit(WHO, Pending(texts=["عايز تيشرت ساده اوفر سايز"]))
        time.sleep(0.5)  # well past the window: still held for the photo
        assert handled == []
        dispatcher.settle_and_submit(WHO, Pending(image_paths=["ref.jpg"]))
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    assert len(handled) == 1
    assert handled[0].texts == ["عايز تيشرت ساده اوفر سايز"]
    assert handled[0].image_paths == ["ref.jpg"]


def test_a_reply_overtaken_by_a_newer_message_is_answered_together_with_it():
    """Turn 1 is composing when the photo arrives. Its reply is not sent; the
    next turn gets the text and the photo in one batch."""
    replies: list[tuple[list[str], list[str]]] = []
    photo_arrived = threading.Event()
    dispatcher: MessageDispatcher

    def handler(key: str, item: Pending) -> None:
        if not replies and not item.image_paths:
            photo_arrived.wait(5)
            if dispatcher.has_newer(key):
                dispatcher.supersede(key, item)
                return
        replies.append((list(item.texts), list(item.image_paths)))

    dispatcher = MessageDispatcher(handler, debounce_seconds=0.05, max_workers=2)
    try:
        dispatcher.settle_and_submit(WHO, Pending(texts=["عايز تيشرت ساده"]))
        time.sleep(0.2)  # turn 1 is running
        dispatcher.announce(WHO)
        photo_arrived.set()
        time.sleep(0.05)
        dispatcher.settle_and_submit(WHO, Pending(image_paths=["ref.jpg"]))
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    assert replies == [(["عايز تيشرت ساده"], ["ref.jpg"])]


def test_a_carried_batch_is_never_dropped():
    """The newer message never became a turn (a duplicate delivery): the
    batch that gave way to it is answered on its own."""
    replies: list[list[str]] = []
    dispatcher: MessageDispatcher

    def handler(key: str, item: Pending) -> None:
        if not replies and dispatcher.has_newer(key) and not item.extras.get("superseded"):
            dispatcher.supersede(key, item)
            return
        replies.append(list(item.texts))

    dispatcher = MessageDispatcher(handler, debounce_seconds=0.05)
    try:
        dispatcher.announce(WHO)
        dispatcher.settle_and_submit(WHO, Pending(texts=["مرحبا"]))
        dispatcher.announce(WHO)
        time.sleep(0.3)
        dispatcher.settle(WHO)  # the second one turned out to need no turn
        assert dispatcher.wait_idle(5)
    finally:
        dispatcher.shutdown()

    assert replies == [["مرحبا"]]


def test_a_superseded_turn_sends_and_stores_nothing(seeded):
    from assistant import session as session_store

    provider = ScriptedProvider([ModelReply(text="عندنا oversized plain t-shirt")])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز تيشرت ساده", provider=provider,
                           superseded=lambda: True)
    assert reply.superseded is True
    assert reply.text == ""
    assert session_store.load(seeded, CHANNEL, WHO) == []


def test_a_turn_that_wrote_something_is_never_superseded(seeded):
    """A cart add, an order, a queue row: the reply describing it must go."""
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "n1", "name": "save_customer_name",
                                    "arguments": {"name": "كريم"}}]),
            ModelReply(text="أهلاً يا كريم"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "كريم", provider=provider,
                           superseded=lambda: True)
    assert reply.superseded is False
    assert reply.text
