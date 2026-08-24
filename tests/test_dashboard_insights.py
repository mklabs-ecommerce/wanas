"""Messaging insights -- the conversation half of the analytics page.

`test_dashboard_stats.py` covers the commerce half (Shopify). Everything
here comes from Postgres, and the test that matters most is the last group:
the daily series must be zero-filled across the whole range, because a chart
with holes in it reads as "no data" where the truth is "no activity".
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import messages as msg, session as session_store
from config.settings import settings
from dashboard import insights_api, web as dashboard
from domain.models import InstagramCommentReply, QueueKind, SessionRow
from domain.services import auth, identities, queues

SECRET = "test-dashboard-secret"


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(insights_api.router)
    return TestClient(app)


@pytest.fixture()
def logged_in(client, seeded):
    auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


def _talk(session, channel, external_id, history):
    identities.get_or_create(session, channel, external_id)
    session_store.save(session, channel, external_id, history)
    session.commit()


def test_requires_login(client):
    assert client.get("/dashboard/api/insights").status_code == 401


def test_range_must_be_one_of_the_presets(logged_in):
    assert logged_in.get("/dashboard/api/insights?days=45").status_code == 400
    assert logged_in.get("/dashboard/api/insights?days=7").status_code == 200


def test_counts_messages_by_who_said_them(logged_in, seeded):
    _talk(seeded, "whatsapp", "201000000001", [
        msg.user("عايز هودي"),
        msg.assistant("عندنا كذا لون", tool_calls=[msg.tool_call("1", "search_products", {})]),
        msg.assistant("اتفضل", by="staff"),
    ])

    totals = logged_in.get("/dashboard/api/insights?days=7").json()["totals"]

    assert totals["conversations"] == 1
    assert totals["customer_messages"] == 1
    assert totals["bot_messages"] == 1
    assert totals["staff_messages"] == 1


def test_reports_the_tools_the_bot_reaches_for(logged_in, seeded):
    _talk(seeded, "whatsapp", "201000000001", [
        msg.assistant("", tool_calls=[msg.tool_call("1", "search_products", {})]),
        msg.assistant("", tool_calls=[msg.tool_call("2", "search_products", {})]),
        msg.assistant("", tool_calls=[msg.tool_call("3", "add_to_cart", {})]),
    ])

    tools = logged_in.get("/dashboard/api/insights?days=7").json()["top_tools"]

    assert tools[0] == {"name": "search_products", "count": 2}
    assert {"name": "add_to_cart", "count": 1} in tools


def test_counts_media_and_tool_errors(logged_in, seeded):
    _talk(seeded, "whatsapp", "201000000001", [
        msg.user("شوف الصورة", images=["data/inbound/x.jpg"]),
        msg.tool_results([msg.tool_result("1", "get_order", {"error": "not_found"})]),
    ])

    totals = logged_in.get("/dashboard/api/insights?days=7").json()["totals"]

    assert totals["media_messages"] == 1
    assert totals["refusals"] == 1


def test_handoff_rate_is_the_share_of_conversations_a_person_took(logged_in, seeded):
    for i in range(4):
        _talk(seeded, "whatsapp", f"20100000000{i}", [msg.user("هاي")])
    queues.enqueue(
        seeded, kind=QueueKind.HANDOFF.value, reason="complaint",
        summary="", channel="whatsapp", external_id="201000000000",
    )
    seeded.commit()

    totals = logged_in.get("/dashboard/api/insights?days=7").json()["totals"]

    assert totals["handoffs"] == 1
    assert totals["handoff_rate"] == 0.25
    assert totals["autonomy_rate"] == 0.75


def test_paused_now_counts_conversations_under_human_control(logged_in, seeded):
    _talk(seeded, "whatsapp", "201000000001", [msg.user("هاي")])
    identities.pause(seeded, "whatsapp", "201000000001")
    seeded.commit()

    assert logged_in.get("/dashboard/api/insights?days=7").json()["totals"]["paused_now"] == 1


def test_instagram_comment_replies_are_reported(logged_in, seeded):
    seeded.add(InstagramCommentReply(
        comment_id="c1", media_id="m1", commenter_igsid="ig-1",
        public_replied=True, private_replied=False,
    ))
    seeded.commit()

    totals = logged_in.get("/dashboard/api/insights?days=7").json()["totals"]

    assert totals["instagram_comments"] == 1
    assert totals["instagram_comment_public_replies"] == 1
    assert totals["instagram_comment_private_replies"] == 0


# --------------------------------------------------------------------------
# the series
# --------------------------------------------------------------------------


def test_daily_series_are_zero_filled_across_the_whole_range(logged_in, seeded):
    _talk(seeded, "whatsapp", "201000000001", [msg.user("هاي")])

    body = logged_in.get("/dashboard/api/insights?days=7").json()

    for key in ("active_conversations_by_day", "new_contacts_by_day",
                "handoffs_by_day", "instagram_comments_by_day"):
        series = body[key]
        assert len(series) == 7, key
        dates = [point["date"] for point in series]
        assert dates == sorted(dates), key
        assert dates[-1] == datetime.now(UTC).date().isoformat(), key


def test_activity_lands_on_today(logged_in, seeded):
    _talk(seeded, "whatsapp", "201000000001", [msg.user("هاي")])

    series = logged_in.get("/dashboard/api/insights?days=7").json()["active_conversations_by_day"]

    assert series[-1]["count"] == 1
    assert all(point["count"] == 0 for point in series[:-1])


def test_conversations_older_than_the_range_are_excluded(logged_in, seeded):
    _talk(seeded, "whatsapp", "201000000001", [msg.user("قديمة")])
    stale = seeded.get(SessionRow, ("whatsapp", "201000000001"))
    stale.updated_at = datetime.now(UTC) - timedelta(days=40)
    seeded.commit()

    assert logged_in.get("/dashboard/api/insights?days=7").json()["totals"]["conversations"] == 0
    assert logged_in.get("/dashboard/api/insights?days=90").json()["totals"]["conversations"] == 1


def test_the_payload_states_its_own_limitation(logged_in):
    """The page has to be able to say on screen that the daily series counts
    conversation activity, not messages -- so the note ships in the payload,
    not only in the module docstring."""
    body = logged_in.get("/dashboard/api/insights?days=7").json()
    assert "per-message timestamps are not stored" in body["note"]
