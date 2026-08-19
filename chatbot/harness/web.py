"""Local chat harness — web version.

The same throwaway scaffolding as the terminal harness, in a browser: it calls
`handle_message(channel, external_id, text)` and renders what comes back. There
is no chatbot logic in this file. No parallel agent, no second tool registry,
no fake replies — if something works here it works on WhatsApp, and if it
breaks here it was already broken.

What it adds over the terminal version is only presentation: RTL bubbles,
inline images for attachments, tool calls and refusals shown as they happen,
and proactive notifications (order confirmation, status pushes) surfaced next
to the conversation instead of in a log.

Not a product surface, and not authenticated — anyone who can reach it can
converse as any identity. Set HARNESS_ENABLED=0 to leave it out of the app.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from backend.db import session_scope
from backend.services import carts, identities, notifications
from chatbot import session as session_store
from chatbot.display import display_history, turn_detail
from chatbot.media_serving import resolve_servable_path
from chatbot.runtime import handle_message

log = logging.getLogger("wanas.harness.web")

router = APIRouter(prefix="/harness", tags=["harness"])

PAGE = Path(__file__).parent / "chat.html"

DEFAULT_IDENTITY = "201000000001"
CHANNEL = "whatsapp"


def _drain_notifications(since: int) -> list[dict]:
    """Proactive messages the Notification service produced during this turn.

    Read after the transaction commits, because that is when they are actually
    sent — an order confirmation must never appear for an order that rolled
    back.
    """
    sender = notifications.get_sender()
    sent = getattr(sender, "sent", None)
    if sent is None:  # the real WhatsApp client is registered; nothing to show
        return []
    fresh = [
        {
            "to": message.to,
            "text": message.text,
            "template": message.template,
            "kind": message.kind,
            "image_path": message.image_path,
        }
        for message in sent[since:]
    ]
    if len(sent) > 500 and hasattr(sender, "clear"):
        sender.clear()
    return fresh


def _sent_count() -> int:
    return len(getattr(notifications.get_sender(), "sent", []))


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/api/state")
def state(identity: str = Query(DEFAULT_IDENTITY), channel: str = Query(CHANNEL)) -> JSONResponse:
    with session_scope() as db:
        return JSONResponse(
            {
                "identity": identity,
                "channel": channel,
                "history": display_history(session_store.load(db, channel, identity)),
                "paused": identities.is_paused(db, channel, identity),
                "cart": carts.cart_payload(db, channel, identity),
            }
        )


@router.post("/api/send")
def send(payload: dict = Body(...)) -> JSONResponse:
    identity = (payload.get("identity") or DEFAULT_IDENTITY).strip()
    channel = (payload.get("channel") or CHANNEL).strip()
    text = payload.get("text") or ""
    image_paths = payload.get("image_paths") or None
    audio_paths = payload.get("audio_paths") or None

    before = _sent_count()
    with session_scope() as db:
        reply = handle_message(
            channel, identity, text, image_paths=image_paths, audio_paths=audio_paths, db=db
        )
        detail = turn_detail(session_store.load(db, channel, identity))
        paused = identities.is_paused(db, channel, identity)
        cart = carts.cart_payload(db, channel, identity)

    return JSONResponse(
        {
            "text": reply.text,
            "attachments": reply.attachments,
            # The picker and the transcript are shown here for the same reason
            # the tool calls are: this is where a developer finds out what the
            # customer would actually have received.
            "interactive": reply.interactive,
            "transcript": reply.transcript,
            "paused": paused,
            "duplicate": reply.duplicate,
            "tool_calls": reply.tool_calls,
            "error": reply.error,
            "tools": detail,
            "notifications": _drain_notifications(before),
            "cart": cart,
        }
    )


@router.post("/api/reset")
def reset(payload: dict = Body(default={})) -> JSONResponse:
    identity = (payload.get("identity") or DEFAULT_IDENTITY).strip()
    channel = (payload.get("channel") or CHANNEL).strip()
    with session_scope() as db:
        session_store.clear(db, channel, identity)
    # The cart is deliberately left alone: it survives a session reset by
    # design, and the harness should show that rather than hide it.
    return JSONResponse({"ok": True})


@router.post("/api/unpause")
def unpause(payload: dict = Body(default={})) -> JSONResponse:
    """Stands in for the staff resolve in the dashboard, which is the only
    thing that un-pauses a real conversation."""
    identity = (payload.get("identity") or DEFAULT_IDENTITY).strip()
    channel = (payload.get("channel") or CHANNEL).strip()
    with session_scope() as db:
        identities.unpause(db, channel, identity)
    return JSONResponse({"ok": True})


@router.get("/media")
def media(path: str = Query(...)):
    """Serve a catalog image so an attachment renders instead of being a path."""
    target = resolve_servable_path(path)
    if target is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return FileResponse(target)


def _main() -> int:  # pragma: no cover - operational helper
    """python -m chatbot.harness.web — serve the app and print the URL."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="chatbot.harness.web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"\n  chat UI: http://{args.host}:{args.port}/harness\n")
    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
