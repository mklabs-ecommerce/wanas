"""A short «اه» after hours of silence answers the offer it was given.

Production, Oct 1-2: the bot recommended M and offered «Rehla Original Tops,
Black, M -- add it?» at 20:59; «اه» came back at 02:32 and got a fresh
greeting. `session.load` archived the whole live context once
`SESSION_EXPIRY_HOURS` (6 in production) passed, so the model saw an empty
history. The window is now floored at 48h, and an expiry carries the last
exchange into the new context instead of handing the model nothing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from assistant import messages as msg, session as session_store
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.runtime import handle_message
from config import settings as settings_module
from config.settings import SESSION_EXPIRY_FLOOR_HOURS, settings
from domain.models import SessionRow, utcnow
from domain.services import carts

CHANNEL = "whatsapp"
WHO = "201000000777"
VARIANT = "rehla-hoodie-s-olive"
OFFER = "مقاسك M. أضيفلك الهودي الزيتي مقاس S؟"


def _age(db, hours: float) -> None:
    row = db.get(SessionRow, (CHANNEL, WHO))
    row.updated_at = utcnow() - timedelta(hours=hours)
    db.flush()
    # A redeploy between the two messages: nothing may live in process memory,
    # so drop everything the ORM session has cached and read the row again.
    db.expire_all()


def _offer(db) -> None:
    handle_message(
        CHANNEL, WHO, "وزني 60 وطولي 170", db=db,
        provider=ScriptedProvider([ModelReply(text=OFFER)]),
    )


def _yes(db) -> ScriptedProvider:
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[{"id": "c1", "name": "add_to_cart",
                                    "arguments": {"variant_id": VARIANT}}]),
            ModelReply(text="تمام، ضفته للشنطة 🛍️"),
        ]
    )
    handle_message(CHANNEL, WHO, "اه", db=db, provider=provider)
    return provider


def _sent_history(provider: ScriptedProvider) -> list[str]:
    return [str(m.get("content")) for m in provider.calls[0][1]]


@pytest.mark.parametrize("gap_hours", [5.5, 30, 47])
def test_yes_after_a_gap_still_sees_the_offer_and_fills_the_cart(seeded, gap_hours):
    _offer(seeded)
    _age(seeded, gap_hours)

    provider = _yes(seeded)

    sent = _sent_history(provider)
    assert "وزني 60 وطولي 170" in sent
    assert OFFER in sent, "the pending offer must reach the model"
    lines = carts.cart_payload(seeded, CHANNEL, WHO)["lines"]
    assert [line["variant_id"] for line in lines] == [VARIANT]


def test_past_expiry_the_last_exchange_is_carried_not_dropped(seeded):
    _offer(seeded)
    _age(seeded, settings.session_expiry_hours + 24)

    provider = _yes(seeded)

    assert OFFER in _sent_history(provider)
    # Archived, never deleted: the full transcript still holds everything.
    contents = [m.get("content") for m in session_store.transcript(seeded, CHANNEL, WHO)]
    assert OFFER in contents and "اه" in contents


def test_carry_never_opens_on_a_tool_result(seeded):
    history = [msg.user("u0"), msg.assistant("a0")]
    for n in range(6):
        history.append(msg.user(f"u{n + 1}"))
        history.append(msg.assistant("", [{"id": "c", "name": "view_cart", "arguments": {}}]))
        history.append(msg.tool_results([{"id": "c", "name": "view_cart", "content": {}}]))
        history.append(msg.assistant(f"a{n + 1}"))
    session_store.save(seeded, CHANNEL, WHO, history)
    _age(seeded, settings.session_expiry_hours + 1)

    live = session_store.load(seeded, CHANNEL, WHO)
    assert live and live[0]["role"] == "user"
    assert live[-1]["content"] == "a6"


def test_expiry_is_never_shorter_than_the_floor(monkeypatch):
    monkeypatch.setenv("SESSION_EXPIRY_HOURS", "6")
    assert settings_module.load_settings().session_expiry_hours == SESSION_EXPIRY_FLOOR_HOURS
    monkeypatch.delenv("SESSION_EXPIRY_HOURS")
    assert settings_module.load_settings().session_expiry_hours == 72
