"""Composition root.

One deployed application with internal modules -- a modular monolith. At this
volume five separately deployed services would be more operational overhead
than the problem needs.

This is the only file that wires the pieces together: it is where the WhatsApp
client is registered as the Notification service's outbound sender, which is
what keeps /domain/ free of any import from /assistant/.

    uvicorn app:app --reload
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from api.public_media import router as public_media_router
from assistant import session as assistant_session
from assistant.channels import instagram as instagram_channel
from assistant.channels.whatsapp import (
    dispatcher as whatsapp_dispatcher,
    register_outbound_sender,
    router as whatsapp_router,
)
from assistant.harness.web import router as harness_router
from config.settings import settings
from dashboard.collections_api import router as dashboard_collections_router
from dashboard.customers_api import router as dashboard_customers_router
from dashboard.inbox_api import router as dashboard_inbox_router
from dashboard.insights_api import router as dashboard_insights_router
from dashboard.inventory_api import router as dashboard_inventory_router
from dashboard.queue_api import router as dashboard_queue_router
from dashboard.settings_api import router as dashboard_settings_router
from dashboard.shopify_api import router as dashboard_shopify_router
from dashboard.stats_api import router as dashboard_stats_router
from dashboard.web import router as dashboard_router
from domain.db import engine, session_scope
from domain.legal import router as legal_router
from domain.models import Base, Product, ShippingRate, Variant
from domain.services import conversation_reset
from domain.services.scheduler import scheduler
from integrations.shopify.webhooks import router as shopify_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wanas")


def _ensure_catalog_seeded() -> None:
    """Import `data/products_seed.json` / `data/governorates.json` if the
    catalog tables are still empty.

    `python manage.py seed` is the documented recovery step, but it is a
    step someone has to remember to run by hand against the right database at
    the right time -- and an unseeded production database is a silent, total
    failure: the process is healthy, `/health` says everything is configured,
    and every single customer search still comes back "we don't have that".
    Both importers are idempotent -- an existing variant's stock and an
    existing governorate's fee are never touched, only ever filled in when
    missing -- so running this on every boot is safe; on an already-seeded
    database it does nothing.
    """
    from domain.seed.governorates import import_governorates
    from domain.seed.products import SeedError, import_products

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
    `integrations/shopify/product_import.py`. Run once per boot, off the
    request path, so a slow or unreachable Shopify never delays startup or a
    customer's reply. Additive-only and idempotent (a product already
    imported is skipped every later run), so re-running it on every deploy is
    safe -- any failure here is logged and swallowed, never raised past this
    function, the same guarantee every other optional piece of startup config
    on this page gets.
    """
    from integrations.shopify.product_import import import_missing_products

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
    `integrations/shopify/webhook_registration.py`. This closes the "nothing is even
    subscribed" half of `SHOPIFY_WEBHOOK_SECRET is not set` on its own, the
    moment both Shopify and a public URL are configured; the secret itself
    still has to come from Shopify Admin by hand (docs/OPERATIONS.md) -- a
    registered-but-unsigned webhook is refused by `integrations/shopify/webhooks.py`
    exactly as it should be, so registering early and safely is not a risk.
    Idempotent, so safe to run on every boot; any failure is logged and
    swallowed, same guarantee as the rest of this page.
    """
    if not settings.public_base_url:
        log.info("PUBLIC_BASE_URL / RAILWAY_PUBLIC_DOMAIN not set: skipping Shopify webhook registration")
        return

    from integrations.shopify.webhook_registration import register_missing

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
    # The one place domain/services/conversation_reset.py learns how to clear
    # chat history, without domain/ ever importing the assistant layer.
    conversation_reset.register_history_clearer(assistant_session.clear)
    # The one place the WhatsApp client becomes the Notification service's
    # sender. Until it does, everything still works against the LogSender.
    register_outbound_sender()
    # Instagram's own client under its own channel key -- never a second
    # registration of WhatsApp's. Inert (logged, not sent) until credentials
    # exist, exactly like WhatsApp above.
    instagram_channel.register_outbound_sender()
    if not settings.instagram_configured:
        log.warning(
            "Instagram is not configured: /webhooks/instagram refuses with 503 "
            "and outbound Instagram messages are logged, not sent."
        )
    else:
        # The 60-day token gets its first refresh check at boot; the scheduler
        # keeps it going afterwards. Failures are logged and alerted inside.
        try:
            from integrations.instagram import token as instagram_token

            if instagram_token.maybe_refresh(force=True):
                log.info("instagram token refreshed at startup")
        except Exception:
            log.exception("startup instagram token refresh check failed")
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
    scheduler.start()
    yield
    scheduler.stop()
    # Let anything still buffered finish rather than dropping a customer's
    # message on a deploy -- on either channel. Missing one of these drops a
    # customer's buffered message on every deploy.
    whatsapp_dispatcher.shutdown(wait=True)
    instagram_channel.dispatcher.shutdown(wait=True)


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
    from integrations.instagram import token as instagram_token

    token_expires_at = instagram_token.expires_at()
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "llm_key_set": bool(settings.llm_api_key),
        "whatsapp_configured": settings.whatsapp_configured,
        "instagram_configured": settings.instagram_configured,
        "instagram_comments": settings.instagram_comments_enabled,
        # The 60-day token's remaining life, so a broken refresh job is
        # visible weeks before the channel goes quiet.
        "instagram_token_expires_at": (
            token_expires_at.isoformat() if token_expires_at else None
        ),
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
# The second first-class channel: same machinery, its own adapter, its own
# webhook. Inert until Meta credentials exist (see the lifespan warning).
app.include_router(instagram_channel.router)
# Public, unauthenticated, and safe by construction: catalog assets only,
# behind an HMAC path token (`api/public_media.py`). Meta's own fetcher
# has no cookie and no session -- this is how Instagram gets a size chart.
app.include_router(public_media_router)
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
    app.include_router(dashboard_collections_router)
    app.include_router(dashboard_inventory_router)
    app.include_router(dashboard_insights_router)
    app.include_router(dashboard_inbox_router)

if settings.harness_enabled:
    app.include_router(harness_router)
