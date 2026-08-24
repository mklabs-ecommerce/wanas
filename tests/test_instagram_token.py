"""The 60-day token refresh (STEP 10).

Instagram's long-lived token dies 60 days after launch with no symptom but
190-series auth errors. These tests pin the machinery that makes that a
non-event: refresh when close to expiry, leave it alone otherwise, alert a
person when the refresh fails, and -- critically -- make the stored row the
token outbound actually uses.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.db import session_scope
from backend.models import IntegrationToken, QueueKind, utcnow
from backend.services import instagram_token, queues


@pytest.fixture(autouse=True)
def fresh_state(db):
    """No token row anywhere and a reset rate limiter for every test.
    Depends on `db` so the table exists."""
    with session_scope() as session:
        row = session.get(IntegrationToken, instagram_token.PROVIDER)
        if row is not None:
            session.delete(row)
    instagram_token._last_attempt_at = None
    yield
    with session_scope() as session:
        row = session.get(IntegrationToken, instagram_token.PROVIDER)
        if row is not None:
            session.delete(row)


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        import json

        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.fixture()
def fake_graph(monkeypatch):
    """Record refresh calls; answer with a plausible Meta response."""
    calls: list[dict] = []
    state = {
        "status_code": 200,
        "body": {"access_token": "refreshed-token", "expires_in": 60 * 86400},
    }

    def fake_get(url, *, params=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        return FakeResponse(state["status_code"], state["body"])

    monkeypatch.setattr(instagram_token.httpx, "get", fake_get)
    return {"calls": calls, "state": state}


def seed_row(*, token="current-token", days_left=None):
    with session_scope() as session:
        expires = utcnow() + timedelta(days=days_left) if days_left is not None else None
        session.merge(
            IntegrationToken(
                provider=instagram_token.PROVIDER,
                access_token=token,
                expires_at=expires,
                refreshed_at=utcnow(),
            )
        )


def read_row():
    with session_scope() as session:
        row = session.get(IntegrationToken, instagram_token.PROVIDER)
        return None if row is None else row.access_token


# --- when it refreshes ------------------------------------------------------


def test_a_token_nine_days_from_expiry_refreshes_and_the_row_updates(fake_graph, fresh_state):
    seed_row(days_left=9)

    assert instagram_token.maybe_refresh(force=True) is True

    assert len(fake_graph["calls"]) == 1
    params = fake_graph["calls"][0]["params"]
    assert params["grant_type"] == "ig_refresh_token"
    assert params["access_token"] == "current-token"

    with session_scope() as session:
        row = session.get(IntegrationToken, instagram_token.PROVIDER)
        assert row.access_token == "refreshed-token"
        assert (instagram_token._aware(row.expires_at) - utcnow()) > timedelta(days=59)

    # And the client now sends what was stored.
    from backend.integrations.instagram_client import InstagramClient

    client = InstagramClient(account_id="17841400000000000")
    assert client.access_token == "refreshed-token"


def test_a_missing_row_with_a_configured_env_token_refreshes_once(
    fake_graph, fresh_state, monkeypatch
):
    monkeypatch.setattr(
        instagram_token,
        "settings",
        dataclasses.replace(
            settings,
            instagram_account_id="17841400000000000",
            instagram_access_token="env-token",
        ),
    )
    assert instagram_token.maybe_refresh(force=True) is True
    assert fake_graph["calls"][0]["params"]["access_token"] == "env-token"
    assert read_row() == "refreshed-token"


def test_a_token_forty_days_out_is_left_alone(fake_graph, fresh_state):
    seed_row(days_left=40)

    assert instagram_token.maybe_refresh(force=True) is False
    assert fake_graph["calls"] == []
    assert read_row() == "current-token"


def test_the_scheduler_job_is_rate_limited_to_one_attempt_per_day(
    fake_graph, fresh_state, monkeypatch
):
    seed_row(days_left=9)
    monkeypatch.setattr(
        instagram_token,
        "settings",
        dataclasses.replace(settings, instagram_access_token="env-token"),
    )

    instagram_token.scheduled_refresh()
    first_calls = len(fake_graph["calls"])
    assert first_calls >= 1

    # A second tick minutes later does nothing -- the rate limiter holds.
    instagram_token.scheduled_refresh()
    assert len(fake_graph["calls"]) == first_calls


def test_an_unknown_expiry_is_treated_as_urgent(fake_graph, fresh_state):
    seed_row(token="current-token", days_left=None)

    assert instagram_token.maybe_refresh(force=True) is True
    assert len(fake_graph["calls"]) == 1


# --- when it fails ----------------------------------------------------------


def test_a_failed_refresh_enqueues_exactly_one_alert(seeded, fresh_state, fake_graph):
    seed_row(days_left=5)
    fake_graph["state"]["status_code"] = 500
    fake_graph["state"]["body"] = {"error": {"message": "auth expired"}}

    assert instagram_token.maybe_refresh(force=True) is False

    with session_scope() as session:
        alerts = [
            i
            for i in queues.open_items(session, QueueKind.ALERT.value)
            if i.reason == "instagram_token_refresh_failed"
        ]
    assert len(alerts) == 1
    assert "auth expired" in (alerts[0].payload or {}).get("detail", "")
    # The row is untouched, so the current token keeps working until it dies.
    assert read_row() == "current-token"


# --- the client reads the DB first ------------------------------------------


def test_the_db_token_beats_the_env_token_in_the_client(fresh_state, monkeypatch):
    seed_row(token="db-token")
    from backend.integrations.instagram_client import InstagramClient

    client = InstagramClient(account_id="17841400000000000")
    assert client.access_token == "db-token"

    # An explicitly passed token still wins -- tests and fakes rely on it.
    explicit = InstagramClient(account_id="x", access_token="explicit-token")
    assert explicit.access_token == "explicit-token"


def test_without_any_row_the_client_falls_back_to_configuration(fresh_state):
    from backend.integrations.instagram_client import InstagramClient

    client = InstagramClient()
    assert client.access_token == settings.instagram_access_token


# --- /health ----------------------------------------------------------------


def test_health_shows_the_token_expiry(seeded, fresh_state):
    seed_row(days_left=12)
    from app import app as composition_root

    client = TestClient(composition_root)
    body = client.get("/health").json()
    assert body["instagram_token_expires_at"] is not None
    parsed = datetime.fromisoformat(body["instagram_token_expires_at"])
    if parsed.tzinfo is None:
        # SQLite stores naive datetimes; the value is UTC by convention.
        parsed = parsed.replace(tzinfo=UTC)
    assert (parsed - datetime.now(UTC)).days >= 11


def test_health_shows_no_expiry_before_the_first_refresh(seeded, fresh_state):
    from app import app as composition_root

    body = TestClient(composition_root).get("/health").json()
    assert body["instagram_token_expires_at"] is None
