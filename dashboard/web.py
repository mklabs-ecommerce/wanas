"""Staff dashboard.

The other half of `request_human`: something that pauses a conversation and
writes a `StaffQueueItem` has existed since Phase 1, but nothing ever read the
queue back or un-paused the conversation except the harness's `/unpause`
stand-in. A customer who triggered a handoff stayed stuck until someone
edited the database by hand.

This is that missing half, not a general admin panel: it lists conversations,
shows one in full, and lets a logged-in staff member either reply to a
*paused* one (which sends the customer a real WhatsApp message, exactly the
way the bot's own replies go out, then resolves the queue item and un-pauses
the conversation) or resolve it without a reply (a false alarm, or one worked
by phone). It cannot inject a message into a conversation the bot still owns
-- see the `not_paused` guard on `reply` below -- because a person and the
model both writing into the same live turn is exactly the race the debounce
lock (`assistant/dispatcher.py`) exists to prevent.

Authenticated, unlike the harness: `Staff` already existed
(`domain/services/auth.py`, `python manage.py create-staff`) with
nowhere to log in. Session is a signed cookie, not a table -- see
`auth.issue_session_token`. With no `DASHBOARD_SESSION_SECRET` configured,
login refuses outright (503) rather than signing cookies with a secret that
changes every restart, the same call `backend/webhooks/shopify.py` makes with
no webhook secret.

The dashboard has since grown well past conversations -- Shopify products/
orders/customers, statistics, the `item_swap`/`alert` review queue, and
feature-flag settings. Rather than this one file growing to hold all of it,
each area is a sibling router in this same package
(`shopify_api.py`, `stats_api.py`, `queue_api.py`, `settings_api.py`,
`customers_api.py`), each using the exact same staff-cookie guard defined in
`dashboard/guard.py`, each included separately in `app.py`. This file
stays scoped to auth and conversations, unchanged by any of that growth.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Body, Cookie, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import and_, or_, select

from assistant import messages as msg, session as session_store
from assistant.display import display_history
from assistant.media_serving import resolve_servable_path
from config.settings import settings
from domain.db import session_scope
from domain.models import ChannelIdentity, QueueKind, SessionRow
from domain.services import (
    auth,
    conversation_reset,
    identities,
    notifications,
    queues,
)

log = logging.getLogger("wanas.dashboard")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_DIR = Path(__file__).parent
LOGIN_PAGE = _DIR / "login.html"
APP_PAGE = _DIR / "dashboard.html"

COOKIE_NAME = "wanas_staff"

#: Conversations are cheap at this store's size; a cap keeps one huge history
#: table from making the list endpoint slow to page through by hand later.
MAX_CONVERSATIONS = 300

#: How much of the last message shows in the conversation list, in characters.
PREVIEW_LENGTH = 80


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


def _staff(db, token: str | None):
    return auth.staff_from_session_token(db, token)


def _unauthenticated() -> JSONResponse:
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


def _not_configured() -> JSONResponse:
    return JSONResponse({"error": "dashboard_not_configured"}, status_code=503)


# --------------------------------------------------------------------------
# reading the queue and the conversation list
# --------------------------------------------------------------------------


def _open_handoffs(db) -> dict[tuple[str, str], object]:
    return {
        (item.channel, item.external_id): item
        for item in queues.open_items(db, QueueKind.HANDOFF.value)
        if item.channel and item.external_id
    }


def _paused_identity_keys(db) -> set[tuple[str, str]]:
    """Every conversation currently under human control, whether the bot
    paused itself for a handoff reason or a staff member took it over
    manually (`/takeover`, no `StaffQueueItem` involved). This -- not
    `_open_handoffs` -- is the actual source of truth for "paused": a
    handoff always sets this same flag (`identities.pause`), but a manual
    takeover sets it with no queue item to show for it."""
    rows = db.execute(
        select(ChannelIdentity.channel, ChannelIdentity.external_id).where(
            ChannelIdentity.paused_until_staff_reply.is_(True)
        )
    ).all()
    return {(row.channel, row.external_id) for row in rows}


def _preview(history: list[dict]) -> str:
    """The last thing either side actually said, for the conversation list --
    a tool call or a refusal is not something a staff member is scanning for
    here."""
    for message in reversed(history):
        role = message.get("role")
        if role == "user" or role == "assistant":
            text = message.get("content") or ""
        else:
            continue
        if text:
            text = " ".join(text.split())
            return text if len(text) <= PREVIEW_LENGTH else text[:PREVIEW_LENGTH] + "…"
    return ""


def _conversation_summary(row: SessionRow, *, paused: bool, handoff) -> dict:
    return {
        "channel": row.channel,
        "external_id": row.external_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "preview": _preview(row.history or []),
        "paused": paused,
        # A manual takeover has no handoff reason to show -- "manual" is a
        # real, distinct value the frontend labels as "staff took over",
        # never confused with the bot's own escalation reasons.
        "reason": handoff.reason if handoff else ("manual" if paused else None),
        "waiting_since": (
            handoff.created_at.isoformat()
            if handoff
            else (row.updated_at.isoformat() if paused and row.updated_at else None)
        ),
    }


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_PAGE.read_text(encoding="utf-8"))


@router.get("", response_class=HTMLResponse)
def app_page() -> HTMLResponse:
    # No server-side auth check here on purpose: the page itself carries no
    # customer data, only the JS shell. `/api/me` is what actually gates
    # anything, on first fetch, and sends an unauthenticated visitor to
    # `/dashboard/login`.
    return HTMLResponse(APP_PAGE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------


@router.post("/api/login")
def login(request: Request, payload: dict = Body(...)) -> JSONResponse:
    if not settings.dashboard_configured:
        return _not_configured()

    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    with session_scope() as db:
        staff = auth.authenticate(db, username, password)
        if staff is None:
            log.warning("dashboard login failed for %r", username)
            return JSONResponse({"error": "invalid_credentials"}, status_code=401)
        log.info("dashboard login: %s", staff.username)
        token = auth.issue_session_token(staff)
        username_out = staff.username

    response = JSONResponse({"ok": True, "username": username_out})
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        # HTTPS in production (Railway), plain HTTP in local dev -- read off
        # the request that just arrived rather than a setting nobody would
        # remember to flip before the first deploy.
        secure=request.url.scheme == "https",
        max_age=settings.dashboard_session_hours * 3600,
    )
    return response


@router.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/api/me")
def me(wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        staff = _staff(db, wanas_staff)
        if staff is None:
            return _unauthenticated()
        return JSONResponse({"username": staff.username})


# --------------------------------------------------------------------------
# conversations
# --------------------------------------------------------------------------


@router.get("/api/conversations")
def conversations(wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        if _staff(db, wanas_staff) is None:
            return _unauthenticated()

        handoffs = _open_handoffs(db)
        paused_keys = _paused_identity_keys(db)
        rows = list(
            db.scalars(
                select(SessionRow).order_by(SessionRow.updated_at.desc()).limit(MAX_CONVERSATIONS)
            ).all()
        )

        # A paused conversation must never go invisible just because enough
        # *other* traffic pushed it out of the top-300-by-recency page --
        # that is exactly how a customer waiting on a human got stuck forever
        # with nothing pointing back at them. Every paused identity (handoff
        # or manual takeover) is fetched by its own key, unbounded, and
        # merged in.
        present = {(row.channel, row.external_id) for row in rows}
        missing = [key for key in (set(handoffs) | paused_keys) if key not in present]
        if missing:
            conditions = [
                and_(SessionRow.channel == channel, SessionRow.external_id == external_id)
                for channel, external_id in missing
            ]
            rows.extend(db.scalars(select(SessionRow).where(or_(*conditions))).all())

        items = [
            _conversation_summary(
                row,
                paused=(row.channel, row.external_id) in paused_keys,
                handoff=handoffs.get((row.channel, row.external_id)),
            )
            for row in rows
        ]

    # Paused conversations first, oldest wait first -- that is the actual
    # queue order a person should work them in. Everything else stays in the
    # recency order the query already returned.
    paused = sorted((c for c in items if c["paused"]), key=lambda c: c["waiting_since"] or "")
    rest = [c for c in items if not c["paused"]]
    return JSONResponse({"conversations": paused + rest, "open_count": len(paused)})


@router.get("/api/conversations/{channel}/{external_id}")
def conversation_detail(
    channel: str, external_id: str, wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        if _staff(db, wanas_staff) is None:
            return _unauthenticated()

        history = session_store.load(db, channel, external_id)
        handoff = _open_handoffs(db).get((channel, external_id))
        paused = identities.is_paused(db, channel, external_id)

        return JSONResponse(
            {
                "channel": channel,
                "external_id": external_id,
                "history": display_history(history),
                "paused": paused,
                "reason": handoff.reason if handoff else ("manual" if paused else None),
                "summary": handoff.summary if handoff else None,
                "payload": handoff.payload if handoff else None,
            }
        )


@router.post("/api/conversations/{channel}/{external_id}/takeover")
def takeover(channel: str, external_id: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """Staff pulls a conversation under manual control without waiting for
    the bot to escalate it itself -- to watch an order in progress, correct
    course before the bot says something wrong, or just answer in person.
    Idempotent: taking over an already-paused conversation is a no-op that
    still answers `ok`, so the frontend never has to check first."""
    with session_scope() as db:
        if _staff(db, wanas_staff) is None:
            return _unauthenticated()
        identities.pause(db, channel, external_id)
    return JSONResponse({"ok": True})


@router.post("/api/conversations/{channel}/{external_id}/reply")
def reply(
    channel: str,
    external_id: str,
    payload: dict = Body(...),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty_text"}, status_code=400)

    with session_scope() as db:
        staff = _staff(db, wanas_staff)
        if staff is None:
            return _unauthenticated()

        handoff = _open_handoffs(db).get((channel, external_id))
        if not identities.is_paused(db, channel, external_id):
            # Only a paused conversation is staff's to answer -- otherwise a
            # reply typed here lands in the middle of a turn the bot is still
            # running, which is exactly the two-writers race the debounce
            # lock exists to prevent.
            return JSONResponse({"error": "not_paused"}, status_code=409)

        sent = notifications.get_sender(channel).send_text(external_id, text)
        if not sent.delivered:
            return JSONResponse({"error": "send_failed", "detail": sent.error}, status_code=502)

        session_store.append(db, channel, external_id, msg.assistant(text, by="staff"))
        if handoff is not None:
            # The thing that needed attention has been answered; the queue
            # item's job is done.
            queues.resolve(db, handoff.queue_id, staff.staff_id)

        # Hand control straight back to the bot. The pause used to outlive
        # the reply so staff could send several messages before calling
        # `/release`, but a pause nothing auto-clears is exactly how a
        # conversation goes permanently silent when someone forgets that
        # last step -- see the WhatsApp silence investigation in
        # OPENCODE_PROGRESS.md. Answering IS the release now; staff who
        # want to keep control take the conversation over again
        # (`/takeover`), which is idempotent and one click away.
        identities.unpause(db, channel, external_id)

    return JSONResponse({"ok": True})


@router.post("/api/conversations/{channel}/{external_id}/release")
def release(channel: str, external_id: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """Hand control back to the bot -- whichever way staff took it: a
    handoff (resolves the queue item too, for the "false alarm, no reply
    needed" case) or a manual takeover (nothing else to clear). No message
    goes out; the next thing the customer sends is what the bot answers."""
    with session_scope() as db:
        staff = _staff(db, wanas_staff)
        if staff is None:
            return _unauthenticated()

        if not identities.is_paused(db, channel, external_id):
            return JSONResponse({"error": "not_paused"}, status_code=409)

        handoff = _open_handoffs(db).get((channel, external_id))
        if handoff is not None:
            queues.resolve(db, handoff.queue_id, staff.staff_id)
        identities.unpause(db, channel, external_id)

    return JSONResponse({"ok": True})


@router.post("/api/conversations/{channel}/{external_id}/reset")
def reset_conversation(
    channel: str, external_id: str, wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    """Wipe this conversation back to how a brand-new customer looks --
    for staff testing the bot repeatedly from the same WhatsApp number. See
    `backend/services/conversation_reset.py` for exactly what is, and is
    not, touched: a real `Client` record and its order history are never
    reachable from here."""
    with session_scope() as db:
        staff = _staff(db, wanas_staff)
        if staff is None:
            return _unauthenticated()
        conversation_reset.reset(db, channel, external_id, staff_id=staff.staff_id)
    return JSONResponse({"ok": True})


@router.get("/media")
def media(path: str = Query(...), wanas_staff: str | None = Cookie(default=None)):
    """The dashboard's own copy of the harness's `/media`, because the harness
    is off in production (`HARNESS_ENABLED=0`) and the dashboard has to work
    without it. Only local catalog files ever come through here -- a Shopify
    photo is already an `http(s)` url the browser loads directly; see
    `_is_url` in `backend/integrations/whatsapp_client.py`."""
    with session_scope() as db:
        if _staff(db, wanas_staff) is None:
            return _unauthenticated()

    target = resolve_servable_path(path)
    if target is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return FileResponse(target)
