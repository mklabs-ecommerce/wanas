"""Per-message times, and whether the customer has actually seen what we sent.

Two facts the transcript could not carry before. A stored message had no time
of its own, so the dashboard could show the order of a conversation but never
when any of it happened; and Meta's `statuses[]` callbacks -- the only thing
that can say a customer has *read* a message -- were parsed for a log line and
thrown away.

The link between the two sides is `mids`: the platform ids a message actually
went out as, already stamped onto the stored message so a customer's "reply to
this" can be resolved (`assistant/quoting.py`). A receipt names the same ids,
and that is the whole join.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import messages as msg, session as session_store
from assistant.channels import whatsapp as adapter
from assistant.display import display_history, supports_receipts

CHANNEL = "whatsapp"
CUSTOMER = "201000000123"
APP_SECRET = "test-app-secret"

#: A read at a known instant, so the assertion is about the value stored and
#: not about whatever the clock said while the test ran.
READ_EPOCH = 1_780_000_000
READ_ISO = datetime.fromtimestamp(READ_EPOCH, UTC).isoformat()


@pytest.fixture()
def configured(monkeypatch):
    import dataclasses

    from config.settings import settings

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
def client(seeded):
    app = FastAPI()
    app.include_router(adapter.router)
    return TestClient(app)


def status_body(
    message_id: str, state: str, *, recipient: str = CUSTOMER, timestamp: int | None = READ_EPOCH
) -> dict:
    """What Meta posts about a message the shop sent."""
    status: dict = {"id": message_id, "status": state, "recipient_id": recipient}
    if timestamp is not None:
        status["timestamp"] = str(timestamp)
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messaging_product": "whatsapp", "statuses": [status]},
                    }
                ],
            }
        ],
    }


def post(client, body: dict):
    raw = json.dumps(body).encode()
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/whatsapp",
        content=raw,
        headers={"content-type": "application/json", "x-hub-signature-256": f"sha256={digest}"},
    )


def _outbound(seeded, text="أهلاً بيك", mids=("wamid.out.1",)):
    """One message from the shop, stored the way a real send stores it: the
    line first, the platform's ids stamped on after delivery."""
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.assistant(text))
    session_store.attach_outbound_ids(seeded, CHANNEL, CUSTOMER, list(mids))
    seeded.commit()


def _stored(seeded):
    """The transcript as it stands, without keeping the write lock.

    Reading through the fixture session opens a transaction, and every SQLite
    transaction here is `BEGIN IMMEDIATE` (see `domain/db.py`) -- so a read
    left open deadlocks the next request, which opens a session of its own.
    Rolling back costs nothing: this reads and never writes.
    """
    seeded.expire_all()
    history = session_store.transcript(seeded, CHANNEL, CUSTOMER)
    seeded.rollback()
    return history


# --- timestamps -----------------------------------------------------------


def test_every_message_is_stored_with_its_own_time():
    for message in (msg.user("عايز هودي"), msg.assistant("أهلاً"), msg.assistant("تم", by="system")):
        stamped = datetime.fromisoformat(message["at"])
        assert stamped.tzinfo is not None, "UTC and aware, like every other time in this system"
        assert abs((datetime.now(UTC) - stamped).total_seconds()) < 60


def test_the_dashboard_shows_each_message_its_own_time(seeded):
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.user("عايز هودي"))
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.assistant("أهلاً بيك"))

    bubbles = display_history(_stored(seeded))
    assert [b["kind"] for b in bubbles] == ["user", "bot"]
    assert all(b["at"] for b in bubbles)


def test_a_message_stored_before_this_existed_reports_no_time(seeded):
    """Nothing is invented for the archive. A message written before messages
    carried a time has none, and says so with `None` -- a guessed time on a
    year-old conversation is worse than an honest gap, because it reads as
    fact."""
    session_store.save(
        seeded, CHANNEL, CUSTOMER, [{"role": "user", "content": "رسالة قديمة"}]
    )

    bubble = display_history(_stored(seeded))[0]
    assert bubble["at"] is None


# --- receipts -------------------------------------------------------------


def test_a_read_receipt_marks_the_message_the_customer_has_seen(client, configured, seeded):
    """The whole path: a stored outbound message, Meta's callback about it,
    and a transcript that now says the customer opened it."""
    _outbound(seeded)

    assert post(client, status_body("wamid.out.1", "read")).status_code == 200

    stored = _stored(seeded)
    assert stored[-1]["receipt"] == {"status": "read", "at": READ_ISO}

    bubble = display_history(stored)[-1]
    assert bubble["receipt"] == "read"
    assert bubble["seen_at"] == READ_ISO


def test_receipts_walk_forward_and_never_back(client, configured, seeded):
    """Meta can deliver `read` before `delivered`, and retries arrive out of
    order. A message the customer has demonstrably opened must not go back to
    showing as merely delivered."""
    _outbound(seeded)

    post(client, status_body("wamid.out.1", "sent"))
    assert _stored(seeded)[-1]["receipt"]["status"] == "sent"

    post(client, status_body("wamid.out.1", "read"))
    assert _stored(seeded)[-1]["receipt"]["status"] == "read"

    post(client, status_body("wamid.out.1", "delivered", timestamp=READ_EPOCH + 60))
    receipt = _stored(seeded)[-1]["receipt"]
    assert receipt["status"] == "read", "an older stage never overwrites a further one"
    assert receipt["at"] == READ_ISO, "and it keeps the time it was actually read"


def test_a_customers_own_message_never_gets_a_seen_state(client, configured, seeded):
    """A receipt is about what the customer has read. Their own message has no
    such thing -- and a stray callback naming it must not invent one."""
    session_store.append(
        seeded, CHANNEL, CUSTOMER, msg.user("عايز هودي", mids=["wamid.in.1"])
    )
    seeded.commit()

    post(client, status_body("wamid.in.1", "read"))

    stored = _stored(seeded)
    assert "receipt" not in stored[0], "the id belongs to an inbound message; nothing to record"
    assert "receipt" not in display_history(stored)[0]


def test_one_reply_sent_as_several_messages_is_seen_when_any_part_is(client, configured, seeded):
    """A reply can leave as the words, then a picker, then a photo -- three
    ids, one stored message. A customer who read any part had the reply on
    screen, so the message is seen rather than partly seen."""
    _outbound(seeded, mids=("wamid.a", "wamid.b", "wamid.c"))

    post(client, status_body("wamid.b", "read"))

    assert _stored(seeded)[-1]["receipt"]["status"] == "read"


def test_a_failed_status_marks_the_message_as_never_delivered(client, configured, seeded):
    """Meta accepting a send is not Meta delivering it. A `failed` callback is
    the authoritative "this never landed", and it drives the same undelivered
    flag a refused send does."""
    _outbound(seeded, text="طلبك اتشحن")

    assert post(client, status_body("wamid.out.1", "failed")).status_code == 200

    assert _stored(seeded)[-1]["delivery"] == "failed"
    assert display_history(_stored(seeded))[-1]["delivery"] == "failed"


def test_a_receipt_for_an_unknown_message_is_accepted_and_ignored(client, configured, seeded):
    """Ordinary, not exceptional: anything sent before receipts were recorded
    has no ids to match. It must not error, and must not touch the
    transcript."""
    _outbound(seeded)
    before = _stored(seeded)

    assert post(client, status_body("wamid.nobody.knows", "read")).status_code == 200

    assert _stored(seeded) == before


def test_a_receipt_finds_its_message_when_meta_names_another_identifier(
    client, configured, seeded
):
    """Meta has two identifier schemes for the same person -- a phone number
    and a business-scoped user id (`common/identifiers.py`) -- and a receipt
    reported under the one the conversation is *not* keyed on would otherwise
    leave that message reading as never seen. The id is globally unique, so
    the message can be found on its own."""
    _outbound(seeded)

    assert post(client, status_body("wamid.out.1", "read", recipient="EG.998877")).status_code == 200

    assert _stored(seeded)[-1]["receipt"]["status"] == "read"


def test_a_receipt_with_no_usable_timestamp_still_records_the_state(client, configured, seeded):
    """The state is the point. A missing or unparseable timestamp falls back
    to now rather than dropping the fact that the customer read it."""
    _outbound(seeded)

    post(client, status_body("wamid.out.1", "read", timestamp=None))

    receipt = _stored(seeded)[-1]["receipt"]
    assert receipt["status"] == "read"
    assert datetime.fromisoformat(receipt["at"]).tzinfo is not None


# --- channels -------------------------------------------------------------


def test_only_whatsapp_claims_to_know_what_the_customer_has_seen():
    """Instagram's read event is a watermark on a webhook field this app does
    not subscribe to -- there is nothing to pin to one message. Showing every
    Instagram message as unseen would be a claim the shop cannot make, so the
    indicator is absent there instead."""
    assert supports_receipts("whatsapp") is True
    assert supports_receipts("instagram_dm") is False
    assert supports_receipts(None) is False


# --- the whole path, through the webhook ----------------------------------


def test_a_real_reply_can_be_marked_seen_end_to_end(client, configured, seeded, monkeypatch):
    """No hand-stamped ids anywhere: a customer writes, the adapter sends a
    reply, Meta names it, and a later receipt for that name finds it.

    This is the join the feature rests on -- the send's own response body is
    the only thing that ever connects a row in `SessionRow.history` to an
    event on Meta's side.
    """
    from assistant.providers import set_provider
    from assistant.providers.fake import RehearsalProvider

    def fake_post(self, payload):
        if payload.get("status") == "read":
            return True, None, None  # the bot's own blue ticks, not a message
        return True, None, "wamid.reply.99"

    monkeypatch.setattr(adapter.WhatsAppClient, "_post", fake_post)
    set_provider(RehearsalProvider())
    try:
        inbound = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Omar"}}],
                                "messages": [
                                    {
                                        "from": CUSTOMER,
                                        "id": "wamid.in.99",
                                        "type": "text",
                                        "timestamp": "1",
                                        "text": {"body": "categories"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        assert post(client, inbound).status_code == 200

        reply = _stored(seeded)[-1]
        assert reply["role"] == "assistant"
        assert reply["mids"] == ["wamid.reply.99"], "the adapter stamped what Meta called it"

        assert post(client, status_body("wamid.reply.99", "read")).status_code == 200
    finally:
        set_provider(None)

    assert _stored(seeded)[-1]["receipt"] == {"status": "read", "at": READ_ISO}


def test_the_new_keys_are_storage_only_and_never_reach_a_provider():
    """`at` and `receipt` are for the dashboard, not the model.

    The message-shape contract (`assistant/messages.py`) rests on every
    translation layer rebuilding its request from `role`/`content`/
    `tool_calls` rather than forwarding whatever is in the dict -- which is
    what lets a key be added for storage without widening what a customer's
    conversation costs in tokens, or what a model can be confused by. Pinned
    here because it is a guarantee made in prose everywhere else.
    """
    from assistant.providers.gemini import GeminiProvider
    from assistant.providers.openrouter import OpenRouterProvider

    history = [
        msg.user("عايز هودي"),
        msg.assistant("أهلاً بيك", mids=["wamid.1"]),
    ]
    history[1]["receipt"] = {"status": "read", "at": READ_ISO}

    rendered = json.dumps(
        [
            OpenRouterProvider(api_key="x")._messages("system", history),
            GeminiProvider(api_key="x")._contents(history),
        ],
        ensure_ascii=False,
    )
    for leaked in ("receipt", "\"at\"", "mids", READ_ISO, "wamid.1"):
        assert leaked not in rendered, f"{leaked} reached the provider payload"


def test_a_receipt_does_not_count_as_conversation_activity(client, configured, seeded):
    """`SessionRow.updated_at` is both the inbox's sort key and the clock the
    six-hour context expiry runs on. A customer opening the chat without
    writing is not a new message: bumping it would silently extend the bot's
    context window every time somebody read a shipping update, and float a
    silent conversation to the top of the inbox as though it had spoken."""
    from domain.models import SessionRow

    _outbound(seeded)
    before = seeded.get(SessionRow, (CHANNEL, CUSTOMER)).updated_at
    seeded.rollback()

    post(client, status_body("wamid.out.1", "read"))

    seeded.expire_all()
    row = seeded.get(SessionRow, (CHANNEL, CUSTOMER))
    assert row.updated_at == before
    assert row.history[-1]["receipt"]["status"] == "read", "the receipt still landed"
