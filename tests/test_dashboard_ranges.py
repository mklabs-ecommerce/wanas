"""The date window both Analytics tabs ask about.

`dashboard/ranges.py` is shared by `stats_api` (Shopify) and `insights_api`
(Postgres) precisely so the two tabs of one page cannot answer about different
fortnights. The tests that matter most here are the historical ones: a custom
range that does *not* end today is the case every "last N days" shortcut in
this codebase used to get silently wrong, by anchoring on `now` and dropping
every real data point outside it.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import insights_api, ranges, stats_api, web as dashboard
from domain.models import ChannelIdentity
from domain.services import auth

SECRET = "test-dashboard-secret"


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------


def test_a_preset_ends_today_and_counts_inclusively():
    window = ranges.parse(days=7)
    assert window.end == datetime.now(UTC).date()
    assert window.days == 7
    assert len(window.each_day()) == 7
    assert window.preset == 7


def test_a_single_day_is_one_day_not_zero():
    window = ranges.parse(start="2026-03-05", end="2026-03-05")
    assert window.days == 1
    assert window.each_day() == ["2026-03-05"]


def test_each_day_walks_the_window_not_the_last_n_days():
    """The whole point of the module. A historical window's series has to be
    labelled with the dates that were asked for."""
    window = ranges.parse(start="2026-01-01", end="2026-01-05")
    assert window.each_day() == [
        "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05",
    ]


def test_bounds_run_to_the_last_instant_of_the_end_day():
    """Otherwise the final day of every custom range is cut off at midnight
    and reads as empty."""
    start, end = ranges.parse(start="2026-01-01", end="2026-01-02").bounds()
    assert start.isoformat().startswith("2026-01-01T00:00:00")
    assert end.date().isoformat() == "2026-01-02"
    assert end.hour == 23 and end.minute == 59


def test_start_after_end_is_refused_not_swapped():
    """Swapping would answer a question nobody asked and look like it worked."""
    with pytest.raises(ranges.BadRange):
        ranges.parse(start="2026-03-10", end="2026-03-01")


def test_one_half_of_a_range_is_refused():
    with pytest.raises(ranges.BadRange):
        ranges.parse(start="2026-03-10")
    with pytest.raises(ranges.BadRange):
        ranges.parse(end="2026-03-10")


def test_a_malformed_date_is_refused():
    with pytest.raises(ranges.BadRange):
        ranges.parse(start="last tuesday", end="2026-03-10")


def test_an_unlisted_preset_is_still_refused():
    with pytest.raises(ranges.BadRange):
        ranges.parse(days=13)


def test_an_absurd_span_is_refused():
    with pytest.raises(ranges.BadRange):
        ranges.parse(start="2020-01-01", end="2026-01-01")


def test_a_custom_range_wins_over_a_preset():
    window = ranges.parse(days=7, start="2026-01-01", end="2026-01-03")
    assert window.days == 3 and window.preset is None


# --------------------------------------------------------------------------
# the endpoints
# --------------------------------------------------------------------------


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(stats_api.router)
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


@pytest.mark.parametrize("path", ["/dashboard/api/stats", "/dashboard/api/insights"])
def test_both_tabs_take_the_same_custom_range(logged_in, path, shopify):
    res = logged_in.get(f"{path}?start=2026-01-01&end=2026-01-10")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["start"] == "2026-01-01"
    assert body["end"] == "2026-01-10"
    assert body["range_days"] == 10
    assert body["preset"] is None


@pytest.mark.parametrize("path", ["/dashboard/api/stats", "/dashboard/api/insights"])
def test_both_tabs_refuse_the_same_bad_ranges(logged_in, path, shopify):
    for query in ("start=2026-03-10&end=2026-03-01", "start=nope&end=2026-03-01",
                  "start=2026-03-01", "days=13"):
        assert logged_in.get(f"{path}?{query}").status_code == 400, query


@pytest.mark.parametrize("path", ["/dashboard/api/stats", "/dashboard/api/insights"])
def test_a_preset_still_works_unchanged(logged_in, path, shopify):
    body = logged_in.get(f"{path}?days=7").json()
    assert body["range_days"] == 7
    assert body["preset"] == 7


def test_a_historical_insights_window_is_labelled_with_the_days_asked_for(logged_in):
    """The bug: `_empty_days` anchored on today, so every point of a
    historical window fell outside the pre-filled keys and the chart drew a
    flat line of zeros across dates that had real activity."""
    body = logged_in.get("/dashboard/api/insights?start=2026-01-01&end=2026-01-05").json()
    dates = [d["date"] for d in body["active_conversations_by_day"]]
    assert dates == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]


def test_insights_excludes_activity_after_the_window_ends(logged_in, seeded):
    """Every filter used to be `>= since` with no upper bound, which is right
    only while the window ends today."""
    now = datetime.now(UTC)
    seeded.add(
        ChannelIdentity(
            channel="whatsapp", external_id="201000000001", first_seen_at=now
        )
    )
    seeded.commit()

    today = now.date()
    inside = logged_in.get(f"/dashboard/api/insights?start={today}&end={today}").json()
    assert inside["totals"]["new_contacts"] == 1

    old_end = (today - timedelta(days=40)).isoformat()
    old_start = (today - timedelta(days=50)).isoformat()
    outside = logged_in.get(f"/dashboard/api/insights?start={old_start}&end={old_end}").json()
    assert outside["totals"]["new_contacts"] == 0
