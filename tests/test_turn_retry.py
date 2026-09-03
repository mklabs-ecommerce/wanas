"""Automatic retry for a turn that fails on its own, with no customer action.

Two layers, matching where a turn can actually stop without a customer-facing
reply -- see the module docstrings on `assistant/agent.py::_generate_with_retry`
and `assistant/turn_retry.py` for the reasoning behind each.

1. **The model call itself** (`assistant/agent.py`). A transient
   `ProviderError` (`rate_limit`, or the unclassified `provider_error` bucket
   that also covers network errors and non-auth HTTP failures) or an
   unexpected exception gets one silent retry, thirty seconds later, before
   either of `run_turn`'s existing `except` clauses is ever reached. `auth` is
   never retried -- a rejected key fails identically the second time.

2. **The whole turn** (`assistant/turn_retry.py`, used by both channel
   adapters' `_deliver`). Anything that crashes *outside* `run_turn`'s own
   guard -- session/identity plumbing, media handling, a Shopify read -- gets
   the same one-retry-after-thirty-seconds treatment before the existing
   crash-fallback path (release the claimed message ids, alert staff, send
   the generic apology) runs.

Both are proven here to actually wait (via the `_sleep` seam each module
exposes, captured rather than really slept -- the suite's own
`no_retry_delay` autouse fixture no-ops it everywhere else) and to give up
after exactly one retry, never looping.

A third thing is proven by its *absence*: a `request_human` handoff -- even
one raised for `unclear` or `out_of_scope`, which `assistant/recovery.py`
lets a customer's own next message take back -- triggers neither retry path.
It is not a crash; `run_turn` returned normally, having done exactly what it
was asked. Retrying that on a timer would be the bot second-guessing a
customer who explicitly asked for a person, or re-asking a question that
already got its answer. See `test_a_request_human_handoff_never_triggers_a_retry`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import agent, turn_retry
from assistant.channels import whatsapp as adapter
from assistant.providers import set_provider
from assistant.providers.base import ModelReply, ProviderError
from assistant.providers.fake import RehearsalProvider, ScriptedProvider
from config.settings import settings
from domain.models import QueueKind
from domain.services import queues

CHANNEL = "whatsapp"
WHO = "201000000001"
APP_SECRET = "test-app-secret"
PHONE = "201000000123"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class FlakyProvider(ScriptedProvider):
    """Raises on the first `fail_first_n` calls to `generate`, then behaves
    like an ordinary `ScriptedProvider`."""

    def __init__(self, script, *, fail_first_n: int = 1, exc_factory=None):
        super().__init__(script)
        self.fail_first_n = fail_first_n
        self.exc_factory = exc_factory or (lambda: ProviderError("boom", kind="rate_limit"))
        self.attempts = 0

    def generate(self, system_prompt: str, history: list[dict], tools: list) -> ModelReply:
        self.attempts += 1
        if self.attempts <= self.fail_first_n:
            raise self.exc_factory()
        return super().generate(system_prompt, history, tools)


def record_sleep(monkeypatch, target: str) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(target, lambda seconds: calls.append(seconds))
    return calls


# --------------------------------------------------------------------------
# 1. The model call: assistant/agent.py::_generate_with_retry
# --------------------------------------------------------------------------


def test_a_rate_limited_model_call_is_retried_and_the_customer_gets_a_real_answer(seeded, monkeypatch):
    calls = record_sleep(monkeypatch, "assistant.agent._sleep")
    provider = FlakyProvider(
        [ModelReply(text="اتفضل، عندنا كذا لون.")],
        fail_first_n=1,
        exc_factory=lambda: ProviderError("rate limited", kind="rate_limit"),
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عندكم هودي؟", provider=provider)

    assert reply.text == "اتفضل، عندنا كذا لون."
    assert reply.error is None
    assert provider.attempts == 2
    assert calls == [30.0]


def test_a_network_error_is_retried_the_same_way(seeded, monkeypatch):
    """The unclassified `provider_error` kind -- what a dropped connection or
    a non-auth HTTP failure raises -- is retried exactly like `rate_limit`."""
    calls = record_sleep(monkeypatch, "assistant.agent._sleep")
    provider = FlakyProvider(
        [ModelReply(text="تمام.")],
        fail_first_n=1,
        exc_factory=lambda: ProviderError("network error talking to OpenRouter: timeout"),
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "السلام عليكم", provider=provider)

    assert reply.text == "تمام."
    assert provider.attempts == 2
    assert calls == [30.0]


def test_an_unexpected_exception_from_the_provider_is_retried_too(seeded, monkeypatch):
    calls = record_sleep(monkeypatch, "assistant.agent._sleep")
    provider = FlakyProvider(
        [ModelReply(text="اتفضل.")],
        fail_first_n=1,
        exc_factory=lambda: RuntimeError("a library hiccup, not a ProviderError"),
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز أشوف الكولكشن", provider=provider)

    assert reply.text == "اتفضل."
    assert provider.attempts == 2
    assert calls == [30.0]


def test_two_consecutive_rate_limits_fall_back_after_exactly_one_retry(seeded, monkeypatch):
    """The retry is not a loop: a second failure reaches the existing
    fallback text unchanged, and there is no third attempt."""
    calls = record_sleep(monkeypatch, "assistant.agent._sleep")
    provider = FlakyProvider([], fail_first_n=99)  # every attempt raises

    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز هودي أسود", provider=provider)

    assert reply.text == agent.RATE_LIMITED
    assert reply.error == "rate_limit"
    assert provider.attempts == 2  # the original attempt, plus exactly one retry
    assert calls == [30.0]  # slept exactly once, not once per attempt


def test_two_consecutive_unexpected_exceptions_fall_back_to_generic_failure(seeded, monkeypatch):
    calls = record_sleep(monkeypatch, "assistant.agent._sleep")
    provider = FlakyProvider(
        [], fail_first_n=99, exc_factory=lambda: RuntimeError("still broken")
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز هودي أسود", provider=provider)

    assert reply.text == agent.GENERIC_FAILURE
    assert reply.error == "provider_crash"
    assert provider.attempts == 2
    assert calls == [30.0]


def test_an_auth_failure_is_never_retried(seeded, monkeypatch):
    """A rejected key fails identically thirty seconds later -- retrying it
    would only make the customer wait longer for the same apology."""
    calls = record_sleep(monkeypatch, "assistant.agent._sleep")
    provider = FlakyProvider(
        [], fail_first_n=99, exc_factory=lambda: ProviderError("bad key", kind="auth")
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز هودي أسود", provider=provider)

    assert reply.text == agent.GENERIC_FAILURE
    assert reply.error == "auth"
    assert provider.attempts == 1  # no retry attempted at all
    assert calls == []  # and therefore nothing waited on


def test_the_retry_sees_the_exact_same_request_both_times(seeded, monkeypatch):
    """Nothing about the conversation may move between the two attempts --
    the retry is the same question asked again, not a new turn."""
    monkeypatch.setattr("assistant.agent._sleep", lambda seconds: None)
    provider = FlakyProvider(
        [ModelReply(text="تمام.")],
        fail_first_n=1,
        exc_factory=lambda: ProviderError("boom", kind="rate_limit"),
    )
    agent.run_turn(seeded, CHANNEL, WHO, "عندكم زيبر أسود؟", provider=provider)
    # FlakyProvider raises before recording a call, so only the successful
    # second attempt is in `.calls` -- there is only ever one real request in
    # flight from the model's point of view, which is the point.
    assert len(provider.calls) == 1


# --------------------------------------------------------------------------
# 2. The whole turn: assistant/turn_retry.py, via the WhatsApp adapter
# --------------------------------------------------------------------------


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "settings",
        dataclasses.replace(
            settings,
            whatsapp_phone_number_id="123456",
            whatsapp_access_token="test-token",
            whatsapp_app_secret=APP_SECRET,
            whatsapp_verify_token="test-verify-token",
        ),
    )


@pytest.fixture()
def sent(monkeypatch):
    outbox: list[dict] = []

    def fake_post(self, payload):
        if payload.get("status") != "read":
            outbox.append(payload)
        return True, None, "sent.1"

    monkeypatch.setattr(adapter.WhatsAppClient, "_post", fake_post)
    return outbox


@pytest.fixture()
def client(seeded):
    set_provider(RehearsalProvider())
    app = FastAPI()
    app.include_router(adapter.router)
    try:
        yield TestClient(app)
    finally:
        set_provider(None)


def webhook_body(text: str, *, message_id: str = "wamid.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": PHONE, "profile": {"name": "Omar"}}],
                            "messages": [
                                {
                                    "from": PHONE,
                                    "id": message_id,
                                    "type": "text",
                                    "timestamp": "1",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def post(client, body: dict):
    raw = json.dumps(body).encode()
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    headers = {"content-type": "application/json", "x-hub-signature-256": f"sha256={digest}"}
    return client.post("/webhooks/whatsapp", content=raw, headers=headers)


def _flaky_handle_message(*, fail_first_n: int, real_reply):
    """A stand-in for `assistant.runtime.handle_message` that raises for its
    first `fail_first_n` calls, then returns `real_reply`."""
    attempts = {"n": 0}

    def fake(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= fail_first_n:
            raise RuntimeError("session/identity plumbing blew up")
        return real_reply

    fake.attempts = attempts
    return fake


def test_a_turn_that_crashes_outside_run_turn_is_retried_and_the_customer_is_answered(
    client, configured, sent, seeded, monkeypatch
):
    from assistant.runtime import RuntimeReply

    calls = record_sleep(monkeypatch, "assistant.turn_retry._sleep")
    fake = _flaky_handle_message(
        fail_first_n=1, real_reply=RuntimeReply(text="اتفضل، عندنا الأسود متاح.")
    )
    monkeypatch.setattr(adapter, "handle_message", fake)

    response = post(client, webhook_body("عندكم هودي أسود؟"))
    assert response.status_code == 200

    # `send_text` wraps outbound Arabic in RTL/bidi marks at the send
    # boundary (`common/bidi.py`); a substring check is what survives that.
    bodies = [m.get("text", {}).get("body", "") for m in sent if m.get("type") == "text"]
    assert len(bodies) == 1
    assert "اتفضل، عندنا الأسود متاح." in bodies[0]
    assert fake.attempts["n"] == 2
    assert calls == [30.0]

    # And nothing about the crash-fallback path fired: no staff alert for a
    # failure that, from the customer's side, never happened.
    seeded.expire_all()
    assert queues.open_items(seeded, QueueKind.ALERT.value) == []


def test_a_turn_that_crashes_twice_falls_back_to_the_existing_crash_behaviour(
    client, configured, sent, seeded, monkeypatch
):
    calls = record_sleep(monkeypatch, "assistant.turn_retry._sleep")
    fake = _flaky_handle_message(fail_first_n=99, real_reply=None)  # never succeeds
    monkeypatch.setattr(adapter, "handle_message", fake)

    response = post(client, webhook_body("عندكم هودي أسود؟"))
    assert response.status_code == 200

    bodies = [m.get("text", {}).get("body", "") for m in sent if m.get("type") == "text"]
    assert len(bodies) == 1
    assert agent.GENERIC_FAILURE in bodies[0]
    assert fake.attempts["n"] == 2  # the original attempt, plus exactly one retry
    assert calls == [30.0]  # slept exactly once, not once per attempt

    seeded.expire_all()
    assert len(queues.open_items(seeded, QueueKind.ALERT.value)) == 1


# --------------------------------------------------------------------------
# 3. request_human is not a crash: neither retry path touches it
# --------------------------------------------------------------------------


def test_a_request_human_handoff_never_triggers_a_retry(seeded, monkeypatch):
    """The bug this guards against: a background timer re-attempting a
    conversation the customer explicitly asked to be handed to a person, or
    re-asking a question that already got the same 'unclear' answer. Neither
    retry mechanism may fire for a normal (non-crashing) `request_human`
    return -- that case is `assistant/recovery.py`'s job, gated on the
    customer's *own* next message, not a timer."""
    agent_sleeps = record_sleep(monkeypatch, "assistant.agent._sleep")

    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {
                        "id": "a",
                        "name": "request_human",
                        "arguments": {"reason": "customer_asked", "summary": "wants a person"},
                    }
                ]
            ),
            ModelReply(text="تمام، حد من الفريق هيرد عليك."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز أكلم حد", provider=provider)

    assert reply.text == "تمام، حد من الفريق هيرد عليك."
    assert agent_sleeps == []  # no retry -- this was never a failure


def test_the_retry_and_the_message_level_recovery_are_independent(seeded, monkeypatch):
    """Both halves of the design coexist without interfering: a crashed turn
    retries itself with no customer involvement; a legitimate handoff waits
    for the customer, exactly as `assistant/recovery.py` already covers."""
    from assistant import recovery
    from assistant.tools.support_tools import raise_handoff
    from domain.services import identities

    raise_handoff(seeded, CHANNEL, WHO, "unclear", "no idea what they meant")
    assert identities.is_paused(seeded, CHANNEL, WHO) is True

    # A crash-retry mechanism must not itself decide to un-pause this --
    # only `recovery.resumable_handoff` (driven by a new customer message)
    # may do that, exactly as before this change.
    item = recovery.resumable_handoff(seeded, CHANNEL, WHO, [])
    assert item is not None
    assert item.reason == "unclear"


# --------------------------------------------------------------------------
# 4. The retry delay itself
# --------------------------------------------------------------------------


def test_both_retry_delays_are_thirty_seconds():
    assert agent.MODEL_RETRY_DELAY_SECONDS == 30.0
    assert turn_retry.RETRY_DELAY_SECONDS == 30.0
