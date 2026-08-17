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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from backend.config import settings
from backend.db import engine
from backend.models import Base
from chatbot.channels.whatsapp import register_outbound_sender
from chatbot.channels.whatsapp import router as whatsapp_router
from chatbot.harness.web import router as harness_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wanas")

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
    yield


app = FastAPI(title="Wanas Gallery", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    """What is wired up and what is still waiting on a credential."""
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "llm_key_set": bool(settings.llm_api_key),
        "whatsapp_configured": settings.whatsapp_configured,
    }


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/health")


app.include_router(whatsapp_router)

if settings.harness_enabled:
    app.include_router(harness_router)
