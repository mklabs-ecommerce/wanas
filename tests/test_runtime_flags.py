"""Runtime feature-flag overrides: the service layer, and the dashboard
settings panel built on top of it.

`backend/config.py`'s `Settings` stays env-only and frozen; these are the
tests that pin down the separate, staff-writable overlay
(`backend/services/runtime_flags.py`) and the fact that it actually changes
what `assistant/media.py` and the WhatsApp adapter do -- an override that only
changed a database row and nothing else would be worse than not building it.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import media
from assistant.providers.fake import ScriptedProvider
from config.settings import settings
from dashboard import settings_api, web as dashboard
from domain.services import (
    auth,
    runtime_flags,
)

SECRET = "test-dashboard-secret"


# --------------------------------------------------------------------------
# the service layer
# --------------------------------------------------------------------------


def test_default_is_the_env_value_when_no_override_exists(seeded):
    assert runtime_flags.get(seeded, "voice_notes_enabled", True) is True
    assert runtime_flags.get(seeded, "voice_notes_enabled", False) is False


def test_a_set_override_wins_over_the_default(seeded, staff):
    runtime_flags.set(seeded, "voice_notes_enabled", False, staff.staff_id)
    assert runtime_flags.get(seeded, "voice_notes_enabled", True) is False


def test_setting_an_unknown_key_is_rejected(seeded, staff):
    with pytest.raises(ValueError):
        runtime_flags.set(seeded, "not_a_real_flag", True, staff.staff_id)


def test_set_records_who_and_when(seeded, staff):
    row = runtime_flags.set(seeded, "image_understanding_enabled", False, staff.staff_id)
    assert row.updated_by == staff.staff_id
    assert row.updated_at is not None


# --------------------------------------------------------------------------
# the flag actually changes bot behaviour
# --------------------------------------------------------------------------


def test_voice_notes_disabled_by_override_stops_transcription(seeded, staff):
    provider = ScriptedProvider()
    provider.push_transcript("مرحبا")
    runtime_flags.set(seeded, "voice_notes_enabled", False, staff.staff_id)

    assert media.transcribe_voice(seeded, provider, "whatever.ogg") == ""
    assert provider.audio_calls == []


# --------------------------------------------------------------------------
# the dashboard settings panel
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
    app.include_router(settings_api.router)
    return TestClient(app)


@pytest.fixture()
def staff(seeded):
    person = auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    return person


@pytest.fixture()
def logged_in(client, staff):
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


def test_flags_list_requires_login(client):
    res = client.get("/dashboard/api/settings/flags")
    assert res.status_code == 401


def test_flags_list_shows_env_default_when_unset(logged_in):
    res = logged_in.get("/dashboard/api/settings/flags")
    assert res.status_code == 200
    flags = {f["key"]: f for f in res.json()["flags"]}
    assert set(flags) == {f.key for f in runtime_flags.KNOWN_FLAGS}
    voice = flags["voice_notes_enabled"]
    assert voice["overridden"] is False
    assert voice["value"] == settings.voice_notes_enabled


def test_toggling_a_flag_persists_and_attributes_the_staff(logged_in):
    res = logged_in.post("/dashboard/api/settings/flags/voice_notes_enabled", json={"value": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["value"] is False
    assert body["overridden"] is True
    assert body["updated_by"] == "sara"

    again = logged_in.get("/dashboard/api/settings/flags")
    voice = {f["key"]: f for f in again.json()["flags"]}["voice_notes_enabled"]
    assert voice["value"] is False


def test_toggling_an_unknown_flag_is_rejected(logged_in):
    res = logged_in.post("/dashboard/api/settings/flags/not_a_real_flag", json={"value": True})
    assert res.status_code == 404


def test_toggling_without_a_boolean_value_is_rejected(logged_in):
    res = logged_in.post("/dashboard/api/settings/flags/voice_notes_enabled", json={"value": "off"})
    assert res.status_code == 400


def test_system_status_requires_login(client):
    assert client.get("/dashboard/api/settings/status").status_code == 401


def test_system_status_reports_configuration(logged_in):
    res = logged_in.get("/dashboard/api/settings/status")
    assert res.status_code == 200
    body = res.json()
    assert "llm_provider" in body
    assert "shopify_configured" in body
