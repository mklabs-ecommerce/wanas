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

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from backend.config import settings
from backend.db import engine, session_scope
from backend.legal import router as legal_router
from backend.models import Base
from backend.webhooks.shopify import router as shopify_router
from chatbot.channels.whatsapp import dispatcher as whatsapp_dispatcher
from chatbot.channels.whatsapp import register_outbound_sender
from chatbot.channels.whatsapp import router as whatsapp_router
from chatbot.dashboard.customers_api import router as dashboard_customers_router
from chatbot.dashboard.queue_api import router as dashboard_queue_router
from chatbot.dashboard.settings_api import router as dashboard_settings_router
from chatbot.dashboard.shopify_api import router as dashboard_shopify_router
from chatbot.dashboard.stats_api import router as dashboard_stats_router
from chatbot.dashboard.web import router as dashboard_router
from chatbot.harness.web import router as harness_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wanas")


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
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
    yield
    # Let anything still buffered finish rather than dropping a customer's
    # message on a deploy.
    whatsapp_dispatcher.shutdown(wait=True)


app = FastAPI(title="Wanas Gallery", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    """What is wired up and what is still waiting on a credential."""
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
    # routers (chatbot/dashboard/*_api.py) rather than one growing file, all
    # under the same guard since they all sit behind the same staff login.
    app.include_router(dashboard_router)
    app.include_router(dashboard_settings_router)
    app.include_router(dashboard_shopify_router)
    app.include_router(dashboard_customers_router)
    app.include_router(dashboard_stats_router)
    app.include_router(dashboard_queue_router)

if settings.harness_enabled:
    app.include_router(harness_router)
