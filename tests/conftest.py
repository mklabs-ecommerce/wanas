"""Shared fixtures.

Every test runs against a real database created from the same models the app
uses. By default that database is this suite's own throwaway SQLite file: the
`db` fixture drops and recreates the whole schema on every test, so an
exported `DATABASE_URL` must never be able to aim that drop at anything else.

The one deliberate way to run the suite against PostgreSQL instead -- which
`tests/test_order_transaction.py` wants before a deploy -- is to set
WANAS_TEST_DATABASE_URL to the target URL. An ambient `DATABASE_URL` is
ignored either way; see `assert_safe_to_drop` for the second half of the
seatbelt.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Must be set before config.settings is imported anywhere. Assigned, not
# setdefault: the fixture below drops the entire schema, so nothing ambient --
# a shell export or the repo's own .env (python-dotenv skips names already
# present) -- may decide what the suite runs on. Default is this suite's own
# throwaway SQLite file. The one deliberate way to aim the suite at
# PostgreSQL instead is WANAS_TEST_DATABASE_URL, read here and nowhere else;
# a plain exported DATABASE_URL is ignored in both modes.
SUITE_SQLITE_URL = f"sqlite:///{PROJECT_ROOT / 'test_wanas.db'}"


def resolve_test_database_url() -> str:
    """The DATABASE_URL the suite forces, per the contract above."""
    opted_in = os.environ.get("WANAS_TEST_DATABASE_URL", "").strip()
    if opted_in:
        return opted_in
    return SUITE_SQLITE_URL


os.environ["DATABASE_URL"] = resolve_test_database_url()
os.environ.setdefault("LLM_PROVIDER", "fake")
# The dispatcher runs inline at zero, so a webhook test can assert on the reply
# on the line after the request instead of sleeping through the debounce window.
os.environ["MESSAGE_DEBOUNCE_SECONDS"] = "0"
# The harness ships switched off now (it is unauthenticated), but its own test
# module needs it mounted. Set here rather than in that module so `app` is only
# ever imported once, with the flag already in place.
os.environ["HARNESS_ENABLED"] = "1"

# The suite must not read the developer's .env. python-dotenv skips any name
# already present in the environment, so pinning these to blank here is what
# makes the run hermetic -- otherwise a real GEMINI_API_KEY or a local
# CHATBOT_DEBUG=1 silently changes what the assertions see, and the same test
# passes on one machine and fails on another.
#: Kept before the blanking below so the opt-in live conversation tests can
#: still reach a real model. Nothing else may read it.
REAL_LLM_KEY = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
REAL_LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("GEMINI_MODEL") or ""

for _name in (
    "LLM_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "LLM_MODEL",
    "GEMINI_MODEL",
    "CHATBOT_DEBUG",
    "LLM_DEBUG_PAYLOAD",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    # A real value here would make the "no secret configured" dashboard tests
    # pass or fail depending on whose .env happens to be sitting next to the
    # repo, rather than on the code.
    "DASHBOARD_SESSION_SECRET",
    # A real store domain here would have the suite quietly making live calls
    # to Shopify -- slow, rate-limited, and dependent on what the shop happens
    # to be selling today. Tests that exercise the live path inject a snapshot
    # with `shopify_catalog.prime` instead.
    "SHOPIFY_STORE_DOMAIN",
    "SHOPIFY_ADMIN_TOKEN",
):
    os.environ[_name] = ""

from sqlalchemy.engine import make_url  # noqa: E402

from backend.db import SessionLocal, engine, normalise_database_url  # noqa: E402
from backend.models import Base  # noqa: E402
from backend.seed.governorates import import_governorates  # noqa: E402
from backend.seed.products import import_products  # noqa: E402

#: Database names that are never a scratch database, however deliberate the
#: opt-in looked. The shop's own database name ("wanas") is fine to allow --
#: dropping it is exactly what the opt-in guard exists to make deliberate.
_PRODUCTION_LIKE_DB_NAMES = {"prod", "production", "live"}


def _looks_like_production(url) -> bool:
    name = (url.database or "").strip().lower()
    return (
        name in _PRODUCTION_LIKE_DB_NAMES
        or name.endswith(("_prod", "_production", "_live"))
    )


def assert_safe_to_drop(engine) -> None:
    """The seatbelt in front of `Base.metadata.drop_all`.

    The `db` fixture drops everything, so it may run only when the engine
    provably points at this suite's own throwaway SQLite file, or at the one
    database named deliberately via WANAS_TEST_DATABASE_URL. Anything else --
    including a PostgreSQL URL inherited from the environment, which the suite
    ignores wholesale -- raises rather than dropping. Even the opt-in target is
    refused when its database name looks like production.
    """
    url = engine.url
    expected = os.path.abspath(str(PROJECT_ROOT / "test_wanas.db"))
    actual = os.path.abspath(url.database) if url.drivername == "sqlite" and url.database else ""
    if actual == expected:
        return
    opted_in = os.environ.get("WANAS_TEST_DATABASE_URL", "").strip()
    if opted_in and url == make_url(normalise_database_url(opted_in)):
        if _looks_like_production(url):
            raise RuntimeError(
                "Refusing to drop the schema: WANAS_TEST_DATABASE_URL points at "
                f"a database named {url.database!r}, which looks like production "
                "rather than a scratch database for the suite to drop. Point the "
                "opt-in at a disposable database."
            )
        return
    raise RuntimeError(
        "Refusing to drop the schema: the test engine points at "
        f"{url.render_as_string(hide_password=True)} instead of this suite's "
        f"own test database ({expected}) or the database named by the "
        "WANAS_TEST_DATABASE_URL opt-in. Every pytest run drops and recreates "
        "every table it touches; pointing it anywhere else destroys that "
        "database. To run the suite against PostgreSQL, set "
        "WANAS_TEST_DATABASE_URL=<url> -- an ambient DATABASE_URL is ignored "
        "by design. Do not bypass this guard."
    )


@pytest.fixture(scope="function")
def db():
    """A fresh schema per test. Cheap at this size, and it keeps a failing
    test from poisoning the next one's stock counts."""
    assert_safe_to_drop(engine)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="function")
def seeded(db):
    import_products(db)
    import_governorates(db)
    db.commit()
    return db


@pytest.fixture(autouse=True)
def shopify(request, monkeypatch):
    """A working in-memory Shopify shelf, installed for every test.

    The order path asks Shopify for permission now, so without this every test
    that places an order would just prove that a missing token refuses. The
    fake is seeded from the catalog rows once the `seeded` fixture has run, so
    the default state is "both sides agree" and a test that wants them to
    disagree says so.

    Opt out with `@pytest.mark.no_shopify` to exercise the outage path.
    """
    from tests.fake_shopify import FakeShopify

    fake = FakeShopify()
    if "no_shopify" not in request.keywords:
        fake.install(monkeypatch)
    if "seeded" in request.fixturenames:
        fake.seed_from(request.getfixturevalue("seeded"))
    return fake


@pytest.fixture()
def cairo_rate(seeded):
    """Shipping fees ship blank on purpose; tests that need a priced
    governorate set one explicitly rather than assuming."""
    from backend.models import ShippingRate

    rate = seeded.get(ShippingRate, "Cairo")
    rate.fee = 60
    seeded.commit()
    return rate
