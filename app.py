"""Composition root.

One deployed application with internal modules -- a modular monolith. At this
volume five separately deployed services would be more operational overhead
than the problem needs.

This is the only file that wires the pieces together: it is where the WhatsApp
client is registered as the Notification service's outbound sender, which is
what keeps /backend/ free of any import from /chatbot/.

    uvicorn app:app --reload
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from backend.config import settings
from backend.db import engine, session_scope
from backend.legal import router as legal_router
from backend.models import Base, Product, ShippingRate, Variant
from backend.webhooks.shopify import router as shopify_router
from chatbot.channels.whatsapp import dispatcher as whatsapp_dispatcher
from chatbot.channels.whatsapp import register_outbound_sender
from chatbot.channels.whatsapp import router as whatsapp_router
from chatbot.harness.web import router as harness_router
from dashboard.customers_api import router as dashboard_customers_router
from dashboard.queue_api import router as dashboard_queue_router
from dashboard.settings_api import router as dashboard_settings_router
from dashboard.shopify_api import router as dashboard_shopify_router
from dashboard.stats_api import router as dashboard_stats_router
from dashboard.web import router as dashboard_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wanas")


def _ensure_catalog_seeded() -> None:
    """Import `data/products_seed.json` / `data/governorates.json` if the
    catalog tables are still empty.

    `python -m backend.cli seed` is the documented recovery step, but it is a
    step someone has to remember to run by hand against the right database at
    the right time -- and an unseeded production database is a silent, total
    failure: the process is healthy, `/health` says everything is configured,
    and every single customer search still comes back "we don't have that".
    Both importers are idempotent -- an existing variant's stock and an
    existing governorate's fee are never touched, only ever filled in when
    missing -- so running this on every boot is safe; on an already-seeded
    database it does nothing.
    """
    from backend.seed.governorates import import_governorates
    from backend.seed.products import SeedError, import_products

    try:
        with session_scope() as db:
            if db.query(Product).count() > 0:
                return
            product_stats = import_products(db)
            gov_stats = import_governorates(db)
    except SeedError:
        log.exception("catalog was empty and the seed data failed its own consistency check")
        return
    except Exception:
        log.exception("could not auto-seed the catalog")
        return

    log.warning(
        "catalog was empty: seeded %s product(s) / %s variant(s) and %s governorate(s) automatically",
        product_stats["products"],
        product_stats["variants"],
        gov_stats["governorates"],
    )


#: The flat rate the shop set for every governorate on 2026-08-20. Only ever
#: applied to a governorate with no fee yet -- staff correcting one later
#: through the dashboard is never overwritten by a later boot of this.
_DEFAULT_SHIPPING_FEE = Decimal("110")


def _ensure_shipping_fees_set() -> None:
    """Fill any governorate still missing a shipping fee with the shop's flat
    rate. Additive-only, same guarantee as `_ensure_catalog_seeded`: a
    governorate that already has a fee -- from a previous run of this, or
    from a staff edit -- is never touched.
    """
    with session_scope() as db:
        rows = db.query(ShippingRate).filter(ShippingRate.fee.is_(None)).all()
        if not rows:
            return
        for row in rows:
            row.fee = _DEFAULT_SHIPPING_FEE
        count = len(rows)

    log.warning(
        "set the default shipping fee (%s EGP) for %d governorate(s) that had none",
        _DEFAULT_SHIPPING_FEE,
        count,
    )


def _import_missing_shopify_products() -> None:
    """A product created straight in Shopify Admin -- not through the
    dashboard's own create panel -- gets no wanas.db row, and the bot's
    search only ever reads wanas.db; see
    `backend/services/shopify_product_import.py`. Run once per boot, off the
    request path, so a slow or unreachable Shopify never delays startup or a
    customer's reply. Additive-only and idempotent (a product already
    imported is skipped every later run), so re-running it on every deploy is
    safe -- any failure here is logged and swallowed, never raised past this
    function, the same guarantee every other optional piece of startup config
    on this page gets.
    """
    from backend.services.shopify_product_import import import_missing_products

    try:
        with session_scope() as db:
            report = import_missing_products(db, apply=True)
    except Exception:
        log.exception("Shopify product import reconciliation failed")
        return

    if report["imported"]:
        log.info(
            "imported %d product(s) from Shopify that had no wanas.db row: %s",
            len(report["imported"]),
            ", ".join(item["title"] for item in report["imported"]),
        )
    for problem in report["problems"]:
        log.warning("Shopify product import: %s", problem)


def _register_shopify_webhooks() -> None:
    """Subscribe Shopify to push order-status changes to this app -- see
    `backend/services/shopify_webhooks.py`. This closes the "nothing is even
    subscribed" half of `SHOPIFY_WEBHOOK_SECRET is not set` on its own, the
    moment both Shopify and a public URL are configured; the secret itself
    still has to come from Shopify Admin by hand (docs/OPERATIONS.md) -- a
    registered-but-unsigned webhook is refused by `backend/webhooks/shopify.py`
    exactly as it should be, so registering early and safely is not a risk.
    Idempotent, so safe to run on every boot; any failure is logged and
    swallowed, same guarantee as the rest of this page.
    """
    if not settings.public_base_url:
        log.info("PUBLIC_BASE_URL / RAILWAY_PUBLIC_DOMAIN not set: skipping Shopify webhook registration")
        return

    from backend.services.shopify_webhooks import register_missing

    callback_url = f"{settings.public_base_url}/webhooks/shopify"
    try:
        report = register_missing(callback_url)
    except Exception:
        log.exception("Shopify webhook registration failed")
        return

    if report["created"]:
        log.info("registered Shopify webhook subscription(s): %s", ", ".join(report["created"]))
    for problem in report["problems"]:
        log.warning("Shopify webhook registration: %s", problem)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    _ensure_catalog_seeded()
    _ensure_shipping_fees_set()
    # The one place the WhatsApp client becomes the Notification service's
    # sender. Until it does, everything still works against the LogSender.
    register_outbound_sender()
    if settings.llm_provider in {"fake", "rehearsal"} or not settings.llm_api_key:
        log.warning("no LLM key configured: the agent will run the rehearsal stand-in")
    if settings.chatbot_debug:
        # Loud on every boot, because the failure mode is someone shipping a
        # .env with the local debugging flag still set and customers seeing
        # raw provider errors in their replies.
        log.warning(
            "CHATBOT_DEBUG is ON: raw provider errors will be shown to customers. "
            "Set CHATBOT_DEBUG=0 before this is reachable by anyone real."
        )
    if settings.llm_debug_payload:
        log.warning("LLM_DEBUG_PAYLOAD is ON: every request body is written to the log.")
    if settings.harness_enabled:
        # Unauthenticated by design -- it is a local testing surface, and
        # anyone who can reach it can converse as any customer identity.
        log.warning("local chat harness mounted at /harness (unauthenticated). HARNESS_ENABLED=0 removes it.")
    if not settings.shopify_webhooks_configured:
        # Without it the shop's own fulfilments never reach the customer, and
        # that failure is silent: orders simply stay `Confirmed` forever.
        log.warning(
            "SHOPIFY_WEBHOOK_SECRET is not set: order status pushes "
            "(packed / shipped / delivered) will never fire."
        )
    if settings.dashboard_enabled and not settings.dashboard_configured:
        # Same failure shape as the line above: a conversation that pauses for
        # a person still pauses, but there is no way left to un-pause it.
        log.warning(
            "DASHBOARD_SESSION_SECRET is not set: /dashboard cannot log anyone "
            "in, so a paused conversation has no way back to the customer."
        )
    log.info(
        "inbound messages: debounce %.1fs across %s workers",
        settings.message_debounce_seconds,
        settings.message_workers,
    )
    if settings.shopify_configured:
        threading.Thread(
            target=_import_missing_shopify_products,
            name="shopify-product-import",
            daemon=True,
        ).start()
        threading.Thread(
            target=_register_shopify_webhooks,
            name="shopify-webhook-register",
            daemon=True,
        ).start()
    yield
    # Let anything still buffered finish rather than dropping a customer's
    # message on a deploy.
    whatsapp_dispatcher.shutdown(wait=True)


app = FastAPI(title="Wanas Gallery", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    """What is wired up and what is still waiting on a credential.

    `catalog_products` / `catalog_variants` are here for the same reason as
    every other field on this page: a silent-but-total failure (an unseeded
    database behind a perfectly healthy process) looks identical to "working"
    from the outside otherwise, and costs a customer conversation to notice.
    """
    with session_scope() as db:
        product_count = db.query(Product).count()
        variant_count = db.query(Variant).count()
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "llm_key_set": bool(settings.llm_api_key),
        "whatsapp_configured": settings.whatsapp_configured,
        "shopify_configured": settings.shopify_configured,
        "shopify_webhooks_configured": settings.shopify_webhooks_configured,
        "voice_notes": settings.voice_notes_enabled,
        "image_understanding": settings.image_understanding_enabled,
        "dashboard_configured": settings.dashboard_configured,
        "catalog_products": product_count,
        "catalog_variants": variant_count,
    }


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/health")


app.include_router(whatsapp_router)
app.include_router(shopify_router)
# Public, unauthenticated: Meta requires a privacy policy URL a logged-out
# reviewer can read.
app.include_router(legal_router)

if settings.dashboard_enabled:
    # Authenticated (a staff login), unlike the harness below -- safe to leave
    # mounted by default. `dashboard_configured` still gates whether login
    # actually works; see the lifespan warning above. Split into sibling
    # routers (dashboard/*_api.py) rather than one growing file, all
    # under the same guard since they all sit behind the same staff login.
    app.include_router(dashboard_router)
    app.include_router(dashboard_settings_router)
    app.include_router(dashboard_shopify_router)
    app.include_router(dashboard_customers_router)
    app.include_router(dashboard_stats_router)
    app.include_router(dashboard_queue_router)

if settings.harness_enabled:
    app.include_router(harness_router)
