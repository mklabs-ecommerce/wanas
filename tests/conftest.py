"""Shared fixtures.

Every test runs against a real database created from the same models the app
uses. By default that database is this suite's own throwaway SQLite file: the
`db` fixture drops and recreates the whole schema on every test, so an
exported `DATABASE_URL` must never be able to aim that drop at anything else.

The one deliberate way to run the suite against PostgreSQL instead -- which
`tests/test_order_transaction.py` wants before a deploy -- is to set
REHLA_TEST_DATABASE_URL to the target URL. An ambient `DATABASE_URL` is
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
# PostgreSQL instead is REHLA_TEST_DATABASE_URL, read here and nowhere else;
# a plain exported DATABASE_URL is ignored in both modes.
SUITE_SQLITE_URL = f"sqlite:///{PROJECT_ROOT / 'test_rehla.db'}"

# The variable was called WANAS_TEST_DATABASE_URL before the rename; a shell or
# CI job still exporting it keeps working until it is changed.
if not os.environ.get("REHLA_TEST_DATABASE_URL", "").strip() and os.environ.get(
    "WANAS_TEST_DATABASE_URL", ""
).strip():
    os.environ["REHLA_TEST_DATABASE_URL"] = os.environ["WANAS_TEST_DATABASE_URL"]


def resolve_test_database_url() -> str:
    """The DATABASE_URL the suite forces, per the contract above."""
    opted_in = os.environ.get("REHLA_TEST_DATABASE_URL", "").strip()
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
# The suite's own fake Shopify answers every store call; the local shelf
# (no-Shopify mode) is opted into per test.
os.environ["LOCAL_STORE"] = "0"

# The suite must not read the developer's .env. python-dotenv skips any name
# already present in the environment, so pinning these to blank here is what
# makes the run hermetic -- otherwise a real GEMINI_API_KEY or a local
# CHATBOT_DEBUG=1 silently changes what the assertions see, and the same test
# passes on one machine and fails on another.
#: The live suite's way back to a real model, read before the blanking below
#: takes the keys away. Nothing else may read it. Every provider the shop can
#: actually be configured with is listed: reading only the Gemini pair
#: silently skipped the live suite on a deployment running OpenRouter, which
#: is this one.
REAL_LLM_KEY = (
    os.getenv("LLM_API_KEY")
    or os.getenv("OPENROUTER_API_KEY")
    or os.getenv("GEMINI_API_KEY")
    or ""
)
REAL_LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("GEMINI_MODEL") or ""
REAL_LLM_PROVIDER = os.getenv("LLM_PROVIDER") or ("gemini" if os.getenv("GEMINI_API_KEY") else "openrouter")

for _name in (
    "LLM_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "LLM_MODEL",
    "GEMINI_MODEL",
    # Same rule as LLM_MODEL, and it was missing for the same reason the
    # Instagram half below was: nothing read this setting until the media
    # calls started honouring it, and the moment they did, a developer with
    # LLM_MEDIA_MODEL in .env graded the provider's media tests against
    # whatever model they happen to run locally.
    "LLM_MEDIA_MODEL",
    "GEMINI_MEDIA_MODEL",
    "CHATBOT_DEBUG",
    "LLM_DEBUG_PAYLOAD",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    # The Instagram half of the same rule, and it was missing: a developer
    # with real INSTAGRAM_ACCOUNT_ID / INSTAGRAM_ACCESS_TOKEN in .env made
    # `settings.instagram_configured` true inside the suite, so the adapter's
    # "inert without credentials" test got a 403 (bad signature) where it
    # asserts a 503. The same test passed in CI and failed on the shop's own
    # machine -- exactly the split this block exists to prevent. The app
    # secret is blanked too, so a signature test can never be graded against
    # a real key.
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_APP_SECRET",
    "INSTAGRAM_VERIFY_TOKEN",
    "INSTAGRAM_APP_SCOPED_ID",
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
    # The suite raises staff-queue items by the hundred, and `queues.enqueue`
    # offers every one of them to the owner-alert mailer. With a developer's
    # real credentials readable here, a green test run would post that many
    # emails to a real inbox -- so the run is hermetic in the same way and for
    # the same reason as the Meta pair above.
    "ALERT_EMAIL_TO",
    "ALERT_SMTP_HOST",
    "ALERT_SMTP_USERNAME",
    "ALERT_SMTP_PASSWORD",
    "ALERT_EMAIL_FROM",
    # ...and the unprefixed aliases config/settings.py also reads, or the
    # blanking above would be a seatbelt with one strap.
    "STORE_OWNER_EMAIL",
    "SMTP_HOST",
    "SMTP_USER",
    "SMTP_PASS",
    "SMTP_FROM",
    # ...and the Gmail API trio, which is the transport that actually
    # delivers from the deploy. Same seatbelt: a green test run must not be
    # able to post hundreds of alerts to a real inbox.
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    "RESEND_API_KEY",
    "RESEND_FROM",
):
    os.environ[_name] = ""

from sqlalchemy.engine import make_url  # noqa: E402

from assistant import session as assistant_session  # noqa: E402
from domain.db import SessionLocal, engine, normalise_database_url  # noqa: E402
from domain.models import Base  # noqa: E402
from domain.seed.governorates import import_governorates  # noqa: E402
from domain.seed.products import import_products  # noqa: E402
from domain.services import conversation_reset  # noqa: E402

# Same registration app.py's lifespan does at startup -- the suite never runs
# app.py's lifespan (dashboard/harness tests mount bare routers), so without
# this, domain/services/conversation_reset.py's reset() would silently skip
# clearing chat history in every test.
conversation_reset.register_history_clearer(assistant_session.clear)

#: Database names that are never a scratch database, however deliberate the
#: opt-in looked. The shop's own database name ("rehla") is fine to allow --
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
    database named deliberately via REHLA_TEST_DATABASE_URL. Anything else --
    including a PostgreSQL URL inherited from the environment, which the suite
    ignores wholesale -- raises rather than dropping. Even the opt-in target is
    refused when its database name looks like production.
    """
    url = engine.url
    expected = os.path.abspath(str(PROJECT_ROOT / "test_rehla.db"))
    actual = os.path.abspath(url.database) if url.drivername == "sqlite" and url.database else ""
    if actual == expected:
        return
    opted_in = os.environ.get("REHLA_TEST_DATABASE_URL", "").strip()
    if opted_in and url == make_url(normalise_database_url(opted_in)):
        if _looks_like_production(url):
            raise RuntimeError(
                "Refusing to drop the schema: REHLA_TEST_DATABASE_URL points at "
                f"a database named {url.database!r}, which looks like production "
                "rather than a scratch database for the suite to drop. Point the "
                "opt-in at a disposable database."
            )
        return
    raise RuntimeError(
        "Refusing to drop the schema: the test engine points at "
        f"{url.render_as_string(hide_password=True)} instead of this suite's "
        f"own test database ({expected}) or the database named by the "
        "REHLA_TEST_DATABASE_URL opt-in. Every pytest run drops and recreates "
        "every table it touches; pointing it anywhere else destroys that "
        "database. To run the suite against PostgreSQL, set "
        "REHLA_TEST_DATABASE_URL=<url> -- an ambient DATABASE_URL is ignored "
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


#: The catalog most tests run against. It is test data -- eighteen products
#: with every shape the code has to handle (a size chart, sleeves, a length
#: axis, a sale price, a sold-out colour) -- not Rehla's own catalog. A
#: module that is about the real catalog sets `USE_REAL_CATALOG = True`.
FIXTURE_CATALOG = PROJECT_ROOT / "tests" / "fixtures" / "catalog"


_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0005fe02fea7d6050f0000000049454e44ae426082"
)
_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "07090908 0a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c"
    "1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ff"
    "c4001f0000010501010101010100000000000000000102030405060708090a0bffc400"
    "b5100002010303020403050504040000017d01020300041105122131410613516107"
    "227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a34"
    "35363738393a434445464748494a535455565758595a636465666768696a7374757677"
    "78797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7"
    "b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4"
    "f5f6f7f8f9faffda0008010100003f00fbfcffd9".replace(" ", "")
)


@pytest.fixture(scope="session", autouse=True)
def catalog_placeholder_files():
    """The picture files the fixture catalog names, as one-pixel stand-ins.

    The servable roots are `data/images` and `data/size-charts` under the
    project, so the files have to sit there; they are created only where
    nothing exists, and removed when the session ends.
    """
    import json

    raw = json.loads((FIXTURE_CATALOG / "products_seed.json").read_text(encoding="utf-8"))
    charts = json.loads((FIXTURE_CATALOG / "size_charts.json").read_text(encoding="utf-8"))
    names: set[str] = set()
    for product in raw:
        names.update(product.get("images") or [])
        for value in (product.get("color_images") or {}).values():
            names.update(value if isinstance(value, list) else [value])
    names.update(c["image"] for c in charts.values() if c.get("image"))
    created: list[Path] = []
    made_dirs: list[Path] = []
    for name in sorted(n for n in names if not n.startswith("http")):
        target = PROJECT_ROOT / name
        if target.exists():
            continue
        for parent in reversed(target.parents):
            if parent == PROJECT_ROOT or parent.exists():
                continue
            parent.mkdir()
            made_dirs.append(parent)
        target.write_bytes(_JPEG if target.suffix.lower() in (".jpg", ".jpeg") else _PNG)
        created.append(target)
    yield
    for target in created:
        target.unlink(missing_ok=True)
    for directory in reversed(made_dirs):
        try:
            directory.rmdir()
        except OSError:
            pass


@pytest.fixture(autouse=True)
def catalog_files(request, monkeypatch):
    if getattr(request.module, "USE_REAL_CATALOG", False):
        return
    import json

    from domain.seed import products as seed_products
    from domain.services import size_charts

    seed_path = FIXTURE_CATALOG / "products_seed.json"
    raw = json.loads(seed_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(seed_products, "SEED_PATH", seed_path)
    monkeypatch.setattr(seed_products, "EXPECTED_PRODUCTS", len(raw))
    monkeypatch.setattr(seed_products, "EXPECTED_VARIANTS", sum(len(p["variants"]) for p in raw))
    monkeypatch.setattr(
        seed_products,
        "EXPECTED_IN_STOCK",
        sum(1 for p in raw for v in p["variants"] if int(v["stock_qty"]) > 0),
    )
    monkeypatch.setattr(size_charts, "CHARTS_PATH", FIXTURE_CATALOG / "size_charts.json")


@pytest.fixture(scope="function")
def seeded(db):
    import_products(db)
    import_governorates(db)
    db.commit()
    return db


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    """Nobody in this suite should ever actually wait out a retry delay.

    `assistant/agent.py::_generate_with_retry` and
    `assistant/turn_retry.py::call_with_retry` both sleep thirty real seconds
    between a failed attempt and the retry -- correct in production, and a
    30-second stall multiplied across every test that provokes a provider
    failure otherwise. A test proving the retry *waits* the right amount
    overrides this itself (its own `monkeypatch.setattr` on the same name
    takes precedence within that test); every other test just gets the fast
    path for free.
    """
    monkeypatch.setattr("assistant.agent._sleep", lambda seconds: None)
    monkeypatch.setattr("assistant.turn_retry._sleep", lambda seconds: None)


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
    from domain.models import ShippingRate

    rate = seeded.get(ShippingRate, "Cairo")
    rate.fee = 60
    seeded.commit()
    return rate


@pytest.fixture(autouse=True)
def name_question_off(request, monkeypatch):
    """A brand-new customer's first replies end with «ممكن أعرف اسم حضرتك؟»
    when the model forgot to ask (`assistant/customer_name.py::ensure_asked`).

    Every conversation in this suite is a brand-new customer, and most tests
    pin a reply's exact text for reasons that have nothing to do with names --
    so the *appended line* is off by default, the same way the Shopify shelf
    is on by default. Everything else about the name (the per-turn note, the
    decision, the tool) still runs. `@pytest.mark.asks_name` turns the line
    back on, and every test about the name uses it.
    """
    if "asks_name" in request.keywords:
        return
    from assistant import customer_name

    monkeypatch.setattr(customer_name, "ensure_asked", lambda text, decision: text)



@pytest.fixture(autouse=True)
def fresh_instagram_caption_cache():
    """Post captions are cached per media id in the Instagram adapter; one
    test's caption must never answer for another's."""
    from assistant.channels import instagram

    instagram._media_cache.clear()
    yield
    instagram._media_cache.clear()
