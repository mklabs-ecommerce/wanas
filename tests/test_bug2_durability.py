"""The Bug 2 durability safeguards.

Root cause of "all chat history gone": with `DATABASE_URL` unset the app
booted on ./wanas.db inside an ephemeral deploy container, and the startup
seed made the empty database look alive. These tests pin the guards against a
repeat: no SQLite boot in a deployed environment unless explicitly allowed,
Railway's bare postgres:// URLs rewritten onto the dialect the repo actually
ships, and a schema drop that lands on the suite's own throwaway file unless
deliberately aimed elsewhere via WANAS_TEST_DATABASE_URL -- which itself
refuses a production-looking database name. None of them need a live
PostgreSQL.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from backend.db import _DEPLOY_MARKERS, normalise_database_url, resolve_database_url
from backend.db import engine as suite_engine
from tests.conftest import SUITE_SQLITE_URL, assert_safe_to_drop, resolve_test_database_url


def test_a_deployment_on_sqlite_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        resolve_database_url("sqlite:///./wanas.db")


def test_the_escape_hatch_allows_sqlite_in_a_deployment(monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "prj_123")
    monkeypatch.setenv("ALLOW_SQLITE_IN_DEPLOY", "1")
    assert resolve_database_url("sqlite:///./wanas.db") == "sqlite:///./wanas.db"


def test_local_development_still_boots_on_sqlite(monkeypatch):
    for marker in _DEPLOY_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    assert resolve_database_url("sqlite:///./wanas.db") == "sqlite:///./wanas.db"


def test_railway_postgres_schemes_are_rewritten_onto_psycopg3():
    assert (
        normalise_database_url("postgres://user:pass@host:5432/wanas")
        == "postgresql+psycopg://user:pass@host:5432/wanas"
    )
    assert (
        normalise_database_url("postgresql://user:pass@host:5432/wanas?sslmode=require")
        == "postgresql+psycopg://user:pass@host:5432/wanas?sslmode=require"
    )
    # Already on the right dialect, or not postgres at all: untouched.
    already = "postgresql+psycopg://user:pass@host:5432/wanas"
    assert normalise_database_url(already) == already
    assert normalise_database_url("sqlite:///./wanas.db") == "sqlite:///./wanas.db"


def test_the_drop_guard_rejects_anything_but_the_suite_s_own_database(tmp_path, monkeypatch):
    monkeypatch.delenv("WANAS_TEST_DATABASE_URL", raising=False)
    stranger = create_engine(f"sqlite:///{tmp_path / 'not_the_test_db.db'}")
    with pytest.raises(RuntimeError, match="Refusing to drop the schema"):
        assert_safe_to_drop(stranger)

    in_memory = create_engine("sqlite://")
    with pytest.raises(RuntimeError, match="Refusing to drop the schema"):
        assert_safe_to_drop(in_memory)

    postgres = create_engine("postgresql+psycopg://user:pass@prod-host:5432/wanas")
    with pytest.raises(RuntimeError, match="Refusing to drop the schema"):
        assert_safe_to_drop(postgres)


def test_the_drop_guard_accepts_the_suite_s_own_engine(monkeypatch):
    monkeypatch.delenv("WANAS_TEST_DATABASE_URL", raising=False)
    assert_safe_to_drop(suite_engine)


def test_an_ambient_database_url_cannot_steer_the_suite(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://user:pass@prod-host:5432/wanas"
    )
    monkeypatch.delenv("WANAS_TEST_DATABASE_URL", raising=False)
    assert resolve_test_database_url() == SUITE_SQLITE_URL


def test_the_opt_in_variable_is_honoured_over_an_ambient_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./ambient_noise.db")
    opt_in = "postgresql://user:pass@localhost:5432/wanas"
    monkeypatch.setenv("WANAS_TEST_DATABASE_URL", opt_in)
    assert resolve_test_database_url() == opt_in
    # And the guard then accepts an engine aimed exactly there (normalised,
    # the same way backend/db.py normalises it at engine creation).
    engine = create_engine(normalise_database_url(opt_in))
    assert_safe_to_drop(engine)


def test_the_drop_guard_refuses_a_url_that_is_neither(monkeypatch, tmp_path):
    monkeypatch.setenv("WANAS_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'chosen.db'}")

    other_sqlite = create_engine(f"sqlite:///{tmp_path / 'not_chosen.db'}")
    with pytest.raises(RuntimeError, match="WANAS_TEST_DATABASE_URL"):
        assert_safe_to_drop(other_sqlite)

    postgres = create_engine("postgresql+psycopg://user:pass@prod-host:5432/wanas")
    with pytest.raises(RuntimeError, match="WANAS_TEST_DATABASE_URL"):
        assert_safe_to_drop(postgres)


def test_the_opt_in_itself_refuses_a_production_looking_name(monkeypatch):
    for name in ("production", "wanas_prod", "live"):
        target = f"postgresql+psycopg://user:pass@localhost:5432/{name}"
        monkeypatch.setenv("WANAS_TEST_DATABASE_URL", target)
        with pytest.raises(RuntimeError, match="looks like production"):
            assert_safe_to_drop(create_engine(target))

    # The shop's own database name is fine to allow once someone has opted in.
    target = "postgresql+psycopg://user:pass@localhost:5432/wanas"
    monkeypatch.setenv("WANAS_TEST_DATABASE_URL", target)
    assert_safe_to_drop(create_engine(target))
