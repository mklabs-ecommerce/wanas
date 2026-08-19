"""Shared fixtures.

Every test runs against a real database created from the same models the app
uses. `DATABASE_URL` is honoured, so the whole suite -- including the
concurrency test -- can be pointed at PostgreSQL without editing a test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Must be set before backend.config is imported anywhere.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'test_wanas.db'}")
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

from backend.db import SessionLocal, engine  # noqa: E402
from backend.models import Base  # noqa: E402
from backend.seed.governorates import import_governorates  # noqa: E402
from backend.seed.products import import_products  # noqa: E402


@pytest.fixture(scope="function")
def db():
    """A fresh schema per test. Cheap at this size, and it keeps a failing
    test from poisoning the next one's stock counts."""
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
