"""The staff dashboard: the other half of `request_human`.

A conversation has been able to pause itself and write a `StaffQueueItem`
since Phase 1 (`chatbot/tools/support_tools.py`); nothing ever read the queue
back or un-paused the conversation except the harness's `/unpause`
stand-in. These tests are the guarantee that a paused conversation actually
reaches a person and comes back -- login, the conversation list, the reply
that both sends and un-pauses, and the "no reply needed" resolution that
still has to un-pause it.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings
from backend.models import QueueKind
from backend.services import auth, identities, notifications, queues
from chatbot import messages as msg
from chatbot import session as session_store
from chatbot.dashboard import web as dashboard

SECRET = "test-dashboard-secret"
CUSTOMER = "201555999111"
CHANNEL = "whatsapp"


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    # `chatbot.dashboard.web` gates login on this; `backend.services.auth`
    # signs and verifies the session token with it. Two separate `settings`
    # names (each module bound its own at import time), so both need patching
    # -- the same reason `test_shopify_webhooks.py` patches the webhook
    # module's own `settings` rather than `backend.config.settings`.
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    return TestClient(app)


@pytest.fixture()
def outbox(monkeypatch):
    sender = notifications.LogSender()
    monkeypatch.setattr(notifications, "_sender", sender)
    return sender.sent


@pytest.fixture()
def staff(seeded):
    person = auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    return person


def login(client, username="sara", password="correct horse battery"):
    return client.post("/dashboard/api/login", json={"username": username, "password": password})


@pytest.fixture()
def logged_in(client, staff):
    res = login(client)
    assert res.status_code == 200, res.text
    return client


def make_paused(seeded, *, reason="customer_asked", summary="عايز يكلم حد", text="عايز أكلم حد"):
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.user(text))
    identities.pause(seeded, CHANNEL, CUSTOMER)
    item = queues.enqueue(
        seeded,
        kind=QueueKind.HANDOFF.value,
        reason=reason,
        summary=summary,
        channel=CHANNEL,
        external_id=CUSTOMER,
        payload={"text": text},
    )
    seeded.commit()
    return item


# --------------------------------------------------------------------------
# configuration and login
# --------------------------------------------------------------------------


def test_login_is_refused_outright_with_no_secret_configured(seeded, staff):
    app = FastAPI()
    app.include_router(dashboard.router)
    client = TestClient(app)
    assert login(client).status_code == 503


def test_a_wrong_password_is_refused(client, staff):
    assert login(client, password="nope").status_code == 401


def test_an_unknown_username_is_refused(client, seeded):
    assert login(client, username="ghost").status_code == 401


def test_a_correct_login_sets_a_cookie_and_reaches_me(client, staff):
    res = login(client)
    assert res.status_code == 200
    assert "wanas_staff" in res.cookies
    me = client.get("/dashboard/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == "sara"


def test_no_cookie_is_unauthenticated(client, staff):
    assert client.get("/dashboard/api/me").status_code == 401
    assert client.get("/dashboard/api/conversations").status_code == 401


def test_a_tampered_cookie_is_rejected(client, staff):
    login(client)
    client.cookies.set("wanas_staff", "1.9999999999.deadbeef")
    assert client.get("/dashboard/api/me").status_code == 401


def test_logout_clears_the_session(logged_in):
    logged_in.post("/dashboard/api/logout")
    assert logged_in.get("/dashboard/api/me").status_code == 401


def test_a_deactivated_staff_account_cannot_use_an_old_cookie(logged_in, seeded, staff):
    staff.is_active = False
    seeded.commit()
    assert logged_in.get("/dashboard/api/me").status_code == 401


# --------------------------------------------------------------------------
# the conversation list
# --------------------------------------------------------------------------


def test_a_fresh_conversation_lists_with_no_open_count(logged_in, seeded):
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.user("عايز هودي"))
    seeded.commit()

    res = logged_in.get("/dashboard/api/conversations")
    assert res.status_code == 200
    body = res.json()
    assert body["open_count"] == 0
    row = next(c for c in body["conversations"] if c["external_id"] == CUSTOMER)
    assert row["paused"] is False
    assert row["preview"] == "عايز هودي"


def test_a_paused_conversation_is_flagged_and_counted(logged_in, seeded):
    make_paused(seeded)

    body = logged_in.get("/dashboard/api/conversations").json()
    assert body["open_count"] == 1
    row = next(c for c in body["conversations"] if c["external_id"] == CUSTOMER)
    assert row["paused"] is True
    assert row["reason"] == "customer_asked"


def test_paused_conversations_sort_ahead_of_everything_else(logged_in, seeded):
    session_store.append(seeded, CHANNEL, "201000000002", msg.user("just browsing"))
    seeded.commit()
    make_paused(seeded)

    ordered = logged_in.get("/dashboard/api/conversations").json()["conversations"]
    assert ordered[0]["external_id"] == CUSTOMER
    assert ordered[0]["paused"] is True


def test_a_paused_conversation_never_disappears_behind_the_recency_cap(logged_in, seeded, monkeypatch):
    """A paused conversation used to be looked up only among the top
    `MAX_CONVERSATIONS` most-recently-updated rows -- so once enough other
    traffic happened, it aged out of that page and vanished from the list even
    though the queue item (and the pause) were both still open. That left the
    customer permanently un-repliable with no sign of them anywhere staff
    would look. The list must surface every open handoff regardless of how
    much has happened since."""
    monkeypatch.setattr(dashboard, "MAX_CONVERSATIONS", 3)
    make_paused(seeded)
    for i in range(5):
        session_store.append(seeded, CHANNEL, f"20100000010{i}", msg.user("hi"))
    seeded.commit()

    body = logged_in.get("/dashboard/api/conversations").json()
    assert body["open_count"] == 1
    ids = [c["external_id"] for c in body["conversations"]]
    assert CUSTOMER in ids
    assert body["conversations"][0]["external_id"] == CUSTOMER


# --------------------------------------------------------------------------
# reading one conversation
# --------------------------------------------------------------------------


def test_conversation_detail_carries_the_handoff_reason_and_history(logged_in, seeded):
    make_paused(seeded, reason="image_received", summary="صورة من عميل")

    res = logged_in.get(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}")
    assert res.status_code == 200
    body = res.json()
    assert body["paused"] is True
    assert body["reason"] == "image_received"
    assert body["summary"] == "صورة من عميل"
    assert body["history"][0]["kind"] == "user"


def test_conversation_detail_exposes_the_handoff_payload(logged_in, seeded):
    """`voice_received` / `image_received` carry the actual file
    (`chatbot/runtime.py`) -- the dashboard has to hand it back, or a staff
    member sees "reply to this person" with nothing to listen to or look at."""
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.user("[صورة]"))
    identities.pause(seeded, CHANNEL, CUSTOMER)
    queues.enqueue(
        seeded,
        kind=QueueKind.HANDOFF.value,
        reason="image_received",
        summary="مش قطعة هدوم",
        channel=CHANNEL,
        external_id=CUSTOMER,
        payload={"images": ["data/inbound/wamid-1.jpg"]},
    )
    seeded.commit()

    body = logged_in.get(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}").json()
    assert body["payload"] == {"images": ["data/inbound/wamid-1.jpg"]}


def test_a_staff_reply_already_in_history_is_labelled_as_such(logged_in, seeded):
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.user("فين طلبي"))
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.assistant("هبعتلك تفاصيل الطلب دلوقتي", by="staff"))
    seeded.commit()

    history = logged_in.get(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}").json()["history"]
    reply = next(item for item in history if item["kind"] == "bot")
    assert reply["by"] == "staff"


# --------------------------------------------------------------------------
# replying
# --------------------------------------------------------------------------


def test_replying_to_a_paused_conversation_sends_appends_and_stays_paused(logged_in, seeded, outbox):
    """A reply resolves the handoff queue item (the thing that needed
    attention has been answered) but must NOT hand control back to the bot
    by itself -- staff may send several more messages before releasing."""
    make_paused(seeded)

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reply", json={"text": "أهلاً، إزاي أقدر أساعدك؟"})
    assert res.status_code == 200

    assert any(m.text == "أهلاً، إزاي أقدر أساعدك؟" and m.to == CUSTOMER for m in outbox)
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is True
    assert queues.open_items(seeded, QueueKind.HANDOFF.value) == []

    history = session_store.load(seeded, CHANNEL, CUSTOMER)
    assert history[-1]["content"] == "أهلاً، إزاي أقدر أساعدك؟"
    assert history[-1]["by"] == "staff"


def test_several_replies_in_a_row_never_return_control_to_the_bot(logged_in, seeded, outbox):
    make_paused(seeded)

    for text in ("لحظة واحدة", "شكلك عايز مقاس L", "تمام، هظبطلك الطلب"):
        res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reply", json={"text": text})
        assert res.status_code == 200
        assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is True
        # That read took a write lock (BEGIN IMMEDIATE on every SQLite
        # transaction, see backend/db.py); release it before the next
        # request opens its own session, or the two deadlock.
        seeded.rollback()

    assert [m.text for m in outbox] == ["لحظة واحدة", "شكلك عايز مقاس L", "تمام، هظبطلك الطلب"]


def test_the_reply_is_attributed_to_the_staff_member_who_sent_it(logged_in, seeded, staff):
    make_paused(seeded)
    logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reply", json={"text": "تمام"})

    resolved = queues.open_items(seeded, QueueKind.HANDOFF.value)
    assert resolved == []  # already resolved
    from backend.models import StaffQueueItem

    item = seeded.query(StaffQueueItem).filter_by(channel=CHANNEL, external_id=CUSTOMER).first()
    assert item.resolved_by == staff.staff_id


def test_an_empty_reply_is_refused(logged_in, seeded):
    make_paused(seeded)
    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reply", json={"text": "   "})
    assert res.status_code == 400
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is True


def test_replying_to_a_conversation_the_bot_still_owns_is_refused(logged_in, seeded):
    """The debounce lock (chatbot/dispatcher.py) exists so exactly one writer
    ever runs a turn at a time -- a staff reply into a live conversation would
    be the second writer."""
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.user("عايز أشوف الكتالوج"))
    seeded.commit()

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reply", json={"text": "أهلاً"})
    assert res.status_code == 409


def test_a_failed_delivery_does_not_unpause_or_resolve(logged_in, seeded, monkeypatch):
    make_paused(seeded)

    class Refusing:
        def send_text(self, to, text, *, template=None):
            from backend.services.notifications import OutboundMessage

            return OutboundMessage(to=to, text=text, delivered=False, error="boom")

    monkeypatch.setattr(notifications, "_sender", Refusing())

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reply", json={"text": "أهلاً"})
    assert res.status_code == 502
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is True
    assert len(queues.open_items(seeded, QueueKind.HANDOFF.value)) == 1


# --------------------------------------------------------------------------
# manual takeover, and returning control to the bot
# --------------------------------------------------------------------------


def test_taking_over_pauses_a_conversation_with_no_queue_item(logged_in, seeded):
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.user("hi"))
    seeded.commit()

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/takeover")
    assert res.status_code == 200
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is True
    assert queues.open_items(seeded, QueueKind.HANDOFF.value) == []
    seeded.rollback()

    body = logged_in.get("/dashboard/api/conversations").json()
    row = next(c for c in body["conversations"] if c["external_id"] == CUSTOMER)
    assert row["paused"] is True
    assert row["reason"] == "manual"


def test_taking_over_an_already_paused_conversation_is_a_harmless_no_op(logged_in, seeded):
    make_paused(seeded)
    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/takeover")
    assert res.status_code == 200
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is True
    # The original handoff item is untouched by a redundant takeover.
    assert len(queues.open_items(seeded, QueueKind.HANDOFF.value)) == 1


def test_release_returns_control_to_the_bot_after_a_manual_takeover(logged_in, seeded, outbox):
    logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/takeover")

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/release")
    assert res.status_code == 200
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is False
    assert outbox == []


def test_release_resolves_the_handoff_item_too(logged_in, seeded, outbox):
    make_paused(seeded)

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/release")
    assert res.status_code == 200
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is False
    assert queues.open_items(seeded, QueueKind.HANDOFF.value) == []
    assert outbox == []


def test_releasing_a_conversation_that_was_never_paused_is_refused(logged_in, seeded):
    session_store.append(seeded, CHANNEL, CUSTOMER, msg.user("hi"))
    seeded.commit()
    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/release")
    assert res.status_code == 409


def test_reply_then_release_is_the_full_multi_message_takeover_flow(logged_in, seeded, outbox):
    make_paused(seeded)
    logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reply", json={"text": "أهلاً"})
    logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reply", json={"text": "تمام كده"})
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is True
    seeded.rollback()

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/release")
    assert res.status_code == 200
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is False
    assert [m.text for m in outbox] == ["أهلاً", "تمام كده"]


# --------------------------------------------------------------------------
# resetting a conversation
# --------------------------------------------------------------------------


def test_reset_clears_history_pause_and_cart(logged_in, seeded):
    from backend.models import CartItem

    make_paused(seeded)
    seeded.add(CartItem(channel=CHANNEL, external_id=CUSTOMER, variant_id="wanas-hoodie-s-olive", quantity=1))
    seeded.commit()

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reset")
    assert res.status_code == 200

    assert session_store.load(seeded, CHANNEL, CUSTOMER) == []
    assert identities.is_paused(seeded, CHANNEL, CUSTOMER) is False
    assert queues.open_items(seeded, QueueKind.HANDOFF.value) == []
    assert seeded.query(CartItem).filter_by(channel=CHANNEL, external_id=CUSTOMER).count() == 0


def test_reset_unlinks_the_client_but_never_touches_the_client_record(logged_in, seeded):
    from backend.models import Client
    from backend.services import identities as identities_service

    client = Client(full_name="Test Customer", phone=CUSTOMER)
    seeded.add(client)
    seeded.flush()
    identity = identities_service.get_or_create(seeded, CHANNEL, CUSTOMER)
    identity.client_id = client.client_id
    identity.pending_link = {"client_id": "pub-1", "matched_on": "phone", "masked_name": "T…"}
    seeded.commit()

    res = logged_in.post(f"/dashboard/api/conversations/{CHANNEL}/{CUSTOMER}/reset")
    assert res.status_code == 200

    seeded.expire_all()
    refreshed_identity = identities_service.get(seeded, CHANNEL, CUSTOMER)
    assert refreshed_identity.client_id is None
    assert refreshed_identity.pending_link is None
    # The Client row itself -- and by extension any real order history --
    # is completely untouched.
    assert seeded.get(Client, client.client_id) is not None


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------


def test_media_requires_login(client, seeded):
    res = client.get("/dashboard/media", params={"path": "data/size-charts/zipup.png"})
    assert res.status_code == 401


def test_media_will_not_serve_anything_outside_the_catalog(logged_in):
    res = logged_in.get("/dashboard/media", params={"path": "../../.env"})
    assert res.status_code == 404


def test_a_catalog_file_is_actually_servable(logged_in):
    res = logged_in.get("/dashboard/media", params={"path": "data/size-charts/zipup.png"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/")


# --------------------------------------------------------------------------
# pages (no auth needed to load the shell; the API gates the data)
# --------------------------------------------------------------------------


def test_the_login_and_app_pages_are_reachable_unauthenticated(client):
    assert client.get("/dashboard/login").status_code == 200
    assert client.get("/dashboard").status_code == 200
