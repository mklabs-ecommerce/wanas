"""The local web chat harness.

It must be a *view* of the existing runtime and nothing more: the same
`handle_message`, the same session, the same tools. These tests mostly exist
to catch it quietly growing chatbot logic of its own.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from assistant.providers import set_provider
from assistant.providers.fake import RehearsalProvider
from domain.db import SessionLocal
from domain.models import Order, QueueKind, ShippingRate, Variant
from domain.services import queues

WHO = "201000000001"
VARIANT = "wanas-hoodie-s-olive"


@pytest.fixture()
def client(seeded):
    from app import app

    # The harness has no provider of its own; it uses whatever the runtime is
    # configured with, exactly as the WhatsApp adapter does.
    set_provider(RehearsalProvider())
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        set_provider(None)


def send(client, text, **extra):
    payload = {"identity": WHO, "text": text}
    payload.update(extra)
    response = client.post("/harness/api/send", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# --- it is the same runtime, not a second one -----------------------------


def test_it_calls_the_real_entry_point(seeded, client, monkeypatch):
    """Guards against the harness ever growing its own reply logic."""
    calls = []
    import assistant.harness.web as web

    real = web.handle_message

    def spy(channel, external_id, text="", **kwargs):
        calls.append((channel, external_id, text))
        return real(channel, external_id, text, **kwargs)

    monkeypatch.setattr(web, "handle_message", spy)
    send(client, "categories")
    assert calls == [("whatsapp", WHO, "categories")]


def test_the_page_is_rtl_and_arabic(client):
    page = client.get("/harness")
    assert page.status_code == 200
    assert 'dir="rtl"' in page.text
    assert 'lang="ar"' in page.text


def test_replies_come_back_as_text(client):
    reply = send(client, "categories")
    assert "T-Shirts" in reply["text"]
    assert reply["error"] is None
    assert reply["tool_calls"] == ["get_categories"]


# --- session behaviour is preserved ---------------------------------------


def test_the_session_survives_a_page_reload(client):
    send(client, "categories")
    state = client.get(f"/harness/api/state?identity={WHO}").json()
    kinds = [item["kind"] for item in state["history"]]
    assert "user" in kinds and "bot" in kinds
    assert state["history"][0]["text"] == "categories"


def test_reset_clears_history_but_not_the_cart(client):
    send(client, f"add {VARIANT} 2")
    assert client.post("/harness/api/reset", json={"identity": WHO}).status_code == 200

    state = client.get(f"/harness/api/state?identity={WHO}").json()
    assert state["history"] == []
    # The cart is stored separately and survives a session reset, by design.
    assert state["cart"]["item_count"] == 2


def test_separate_identities_have_separate_conversations(client):
    send(client, "categories")
    other = client.post(
        "/harness/api/send", json={"identity": "20155555555", "text": "cart"}
    ).json()
    assert "الشنطة فاضية" in other["text"]
    assert client.get("/harness/api/state?identity=20155555555").json()["cart"]["item_count"] == 0


# --- tools, refusals, attachments -----------------------------------------


def test_a_refusal_is_surfaced_with_its_payload(client):
    """`out_of_stock` is only actionable with its alternatives, so the UI gets
    the whole refusal, not just the code."""
    reply = send(client, "add wanas-hoodie-m-olive")
    refusal = reply["tools"][0]
    assert refusal["name"] == "add_to_cart"
    assert refusal["error"] == "out_of_stock"
    assert refusal["content"]["alternatives"]


def test_a_successful_tool_call_is_reported_without_an_error(client):
    reply = send(client, "variants wanas-hoodie")
    assert reply["tools"][0]["name"] == "get_variants"
    assert reply["tools"][0]["error"] is None


def test_a_size_chart_comes_back_as_an_attachment(client):
    reply = send(client, "size wanas-sweatpant")
    assert reply["attachments"] == ["data/size-charts/wide-leg-sweatpants.png"]
    # ...and the numbers are in the text too, for anyone who never opens it.
    assert "31" in reply["text"]


def test_the_attachment_is_actually_servable(client):
    reply = send(client, "size wanas-sweatpant")
    media = client.get("/harness/media", params={"path": reply["attachments"][0]})
    assert media.status_code == 200
    assert media.headers["content-type"].startswith("image/")


@pytest.mark.parametrize(
    "path",
    ["../../.env", "..\\..\\.env", "/etc/passwd", ".env", "config/settings.py", "data/products_seed.json"],
)
def test_media_will_not_serve_anything_outside_the_catalog(client, path):
    response = client.get("/harness/media", params={"path": path})
    assert response.status_code in (403, 404)


# --- proactive notifications ----------------------------------------------


def test_an_order_surfaces_its_proactive_confirmation(client, seeded):
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.commit()

    send(client, f"add {VARIANT} 1")
    reply = send(client, "order Omar | Cairo | 5 Test Street | 01000000000")

    assert "WNS-1001" in reply["text"]
    templates = [n["template"] for n in reply["notifications"]]
    assert "order_confirmation" in templates
    body = next(n for n in reply["notifications"] if n["template"] == "order_confirmation")["text"]
    assert "710" in body

    with SessionLocal() as db:
        assert db.get(Order, "WNS-1001").status == "Confirmed"
        assert db.get(Variant, VARIANT).stock_qty == 9


def test_notifications_are_only_shown_after_the_transaction_commits(client, seeded):
    """A confirmation must never appear for an order that rolled back."""
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.commit()
    send(client, f"add {VARIANT} 1")
    # Governorate with no rate: the order is refused, so nothing is sent.
    reply = send(client, "order Omar | Aswan | 5 Test Street | 01000000000")
    assert reply["notifications"] == []
    with SessionLocal() as db:
        assert db.query(Order).count() == 0


# --- pause and photos -----------------------------------------------------


def test_a_photo_goes_to_a_human_and_pauses_the_conversation(client, seeded):
    reply = send(client, "", image_paths=["data/inbound/photo.jpg"])
    assert reply["paused"] is True
    assert "الفريق" in reply["text"]

    seeded.expire_all()
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "image_received"
    assert item.payload["images"] == ["data/inbound/photo.jpg"]


def test_a_paused_conversation_gets_no_reply_until_a_staff_action(client, seeded):
    send(client, "human customer_asked عايز أكلم حد")
    silent = send(client, "في حد؟")
    assert silent["paused"] is True
    assert silent["text"] is None

    # Stands in for the dashboard resolve, which is the only real way out.
    assert client.post("/harness/api/unpause", json={"identity": WHO}).status_code == 200
    back = send(client, "categories")
    assert back["paused"] is False
    assert "T-Shirts" in back["text"]


# --- mounting -------------------------------------------------------------


def test_the_harness_can_be_switched_off(monkeypatch):
    """It is unauthenticated, so a deployment has to be able to drop it."""
    import dataclasses

    # Off unless a developer asks for it: forgetting an environment variable
    # must not be what exposes an unauthenticated chat surface. (This suite
    # switches it on explicitly -- see tests/conftest.py.)
    from config.settings import load_settings, settings

    monkeypatch.delenv("HARNESS_ENABLED", raising=False)
    assert load_settings().harness_enabled is False

    switched_on = dataclasses.replace(settings, harness_enabled=True)
    assert switched_on.harness_enabled is True

