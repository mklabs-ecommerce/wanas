"""Twenty customers at once, through the real webhook and the real dispatcher.

Each customer's history, cart and name must stay theirs, and one customer
firing several messages in a burst must be answered one turn at a time with
nothing lost. The model is a thread-safe stand-in that answers each history
with what *that* history says, so any cross-talk shows up in the transcript.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import session as session_store
from assistant.channels import whatsapp as adapter
from assistant.dispatcher import MessageDispatcher
from assistant.providers import set_provider
from assistant.providers.base import LLMProvider, ModelReply
from config.settings import settings
from domain.db import SessionLocal
from domain.models import ChannelIdentity, Variant
from domain.services import carts

APP_SECRET = "test-app-secret"
CHANNEL = "whatsapp"
CUSTOMERS = 20
_ID = re.compile(r"C(\d{2})")
NAMES = ["أحمد", "منى", "سارة", "ياسمين", "محمد", "هبة", "علي", "نور", "مريم", "كريم",
         "دينا", "عمر", "رنا", "يوسف", "سلمى", "حسن", "ليلى", "طارق", "هالة", "زياد"]


class EchoProvider(LLMProvider):
    """Answers from the history it is handed, and nothing else.

    First hop of a turn: add this customer's variant and save their name.
    Second hop: a reply listing every customer id the history mentions -- a
    leak anywhere puts a second id in it.
    """

    name = "echo"

    def __init__(self, variants: dict[str, str]):
        self.variants = variants
        self.lock = threading.Lock()
        self.active: dict[str, int] = {}
        self.overlap: list[str] = []

    def generate(self, system_prompt, history, tools):
        user_text = " ".join(
            str(m.get("content") or "") for m in history if m.get("role") == "user"
        )
        ids = sorted(set(_ID.findall(user_text)))
        who = ids[-1] if ids else "??"
        with self.lock:
            self.active[who] = self.active.get(who, 0) + 1
            if self.active[who] > 1:
                self.overlap.append(who)
        try:
            if history and history[-1].get("role") == "tool_results":
                return ModelReply(text="شايف: " + ",".join(f"C{i}" for i in ids))
            last = str(history[-1].get("content") or "")
            if "اسمي" in last:
                return ModelReply(tool_calls=[
                    {"id": "a", "name": "add_to_cart", "arguments": {"variant_id": self.variants[who]}},
                    {"id": "n", "name": "save_customer_name", "arguments": {"name": NAMES[int(who)]}},
                ])
            return ModelReply(text="شايف: " + ",".join(f"C{i}" for i in ids))
        finally:
            with self.lock:
                self.active[who] -= 1


def phone(n: int) -> str:
    return f"2010000{n:05d}"


def body(n: int, text: str, mid: str) -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": phone(n), "profile": {"name": f"P{n}"}}],
            "messages": [{"from": phone(n), "id": mid, "type": "text", "timestamp": "1",
                          "text": {"body": text}}],
        }}]}],
    }).encode()


def headers(raw: bytes) -> dict:
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return {"content-type": "application/json", "x-hub-signature-256": f"sha256={digest}"}


@pytest.fixture()
def wired(seeded, monkeypatch):
    monkeypatch.setattr(adapter, "settings", dataclasses.replace(
        settings,
        whatsapp_phone_number_id="123456",
        whatsapp_access_token="test-token",
        whatsapp_app_secret=APP_SECRET,
        whatsapp_verify_token="v",
    ))
    outbox: list[dict] = []
    out_lock = threading.Lock()

    def fake_post(self, payload):
        if payload.get("status") != "read":
            with out_lock:
                outbox.append(payload)
        return True, None, f"sent.{len(outbox)}"

    monkeypatch.setattr(adapter.WhatsAppClient, "_post", fake_post)
    monkeypatch.setattr(adapter.WhatsAppClient, "_upload", lambda self, path: "media")

    in_stock = [
        v.variant_id
        for v in seeded.query(Variant).filter(Variant.stock_qty > 2).order_by(Variant.variant_id).all()
    ]
    variants = {f"{n:02d}": in_stock[n % len(in_stock)] for n in range(CUSTOMERS)}
    # Every SQLite transaction here is BEGIN IMMEDIATE; an open one on the
    # fixture's session would hold the write lock against every worker.
    seeded.commit()
    provider = EchoProvider(variants)
    set_provider(provider)

    # The production dispatcher, with a real debounce and real worker threads
    # (the suite otherwise runs turns inline with debounce 0).
    dispatcher = MessageDispatcher(adapter._deliver, debounce_seconds=0.3, max_workers=8)
    monkeypatch.setattr(adapter, "dispatcher", dispatcher)

    app = FastAPI()
    app.include_router(adapter.router)
    try:
        yield app, dispatcher, provider, variants, outbox
    finally:
        dispatcher.shutdown()
        set_provider(None)


def test_twenty_customers_at_once_never_share_state(wired):
    app, dispatcher, provider, variants, outbox = wired

    def customer(n: int) -> int:
        raw = body(n, f"أنا C{n:02d} اسمي {NAMES[n]}", f"wamid.{n}")
        with TestClient(app) as client:
            return client.post("/webhooks/whatsapp", content=raw, headers=headers(raw)).status_code

    with ThreadPoolExecutor(max_workers=CUSTOMERS) as pool:
        assert list(pool.map(customer, range(CUSTOMERS))) == [200] * CUSTOMERS
    assert dispatcher.wait_idle(60)

    with SessionLocal() as db:
        for n in range(CUSTOMERS):
            who = f"{n:02d}"
            history = session_store.transcript(db, CHANNEL, phone(n))
            said = " ".join(str(m.get("content") or "") for m in history)
            assert set(_ID.findall(said)) == {who}, f"C{who} saw {said!r}"
            lines = carts.cart_payload(db, CHANNEL, phone(n))["lines"]
            assert [line["variant_id"] for line in lines] == [variants[who]]
            identity = db.get(ChannelIdentity, (CHANNEL, phone(n))) if _keyed() else (
                db.query(ChannelIdentity).filter_by(channel=CHANNEL, external_id=phone(n)).one()
            )
            assert identity.customer_name == NAMES[n]

    by_recipient: dict[str, list[str]] = {}
    for payload in outbox:
        text = (payload.get("text") or {}).get("body", "")
        by_recipient.setdefault(payload.get("to", ""), []).append(text)
    for n in range(CUSTOMERS):
        sent = " ".join(by_recipient.get(phone(n), []))
        assert set(_ID.findall(sent)) <= {f"{n:02d}"}, sent
    assert provider.overlap == []


def test_one_customer_bursting_is_answered_one_turn_at_a_time(wired):
    app, dispatcher, provider, _variants, _outbox = wired
    n = 3

    def fire(k: int) -> int:
        raw = body(n, f"C{n:02d} رسالة {k}", f"wamid.burst.{k}")
        with TestClient(app) as client:
            return client.post("/webhooks/whatsapp", content=raw, headers=headers(raw)).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(fire, range(6))) == [200] * 6
    assert dispatcher.wait_idle(60)

    assert provider.overlap == [], "two turns of one conversation ran at once"
    with SessionLocal() as db:
        history = session_store.transcript(db, CHANNEL, phone(n))
    user_text = " ".join(str(m.get("content") or "") for m in history if m.get("role") == "user")
    for k in range(6):
        assert f"رسالة {k}" in user_text, f"message {k} was lost"
    assert not [m for m in history if m.get("role") == "user" and m.get("provisional")], (
        "every provisional copy was folded into a turn"
    )


def _keyed() -> bool:
    from sqlalchemy import inspect

    return len(inspect(ChannelIdentity).primary_key) == 2
