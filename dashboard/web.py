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
changes every restart, the same call `integrations/shopify/webhooks.py` makes with
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
from assistant.display import display_history, supports_receipts
from assistant.media_serving import resolve_servable_path
from common.identifiers import is_phone_number
from config.settings import settings
from dashboard.guard import require_permission
from domain.db import session_scope
from domain.models import ChannelIdentity, Client, QueueKind, SessionRow
from domain.services import (
    auth,
    conversation_reset,
    identities,
    notifications,
    queues,
    staff_admin,
)

log = logging.getLogger("wanas.dashboard")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_DIR = Path(__file__).parent
LOGIN_PAGE = _DIR / "login.html"
APP_PAGE = _DIR / "dashboard.html"
#: The brand mark, served rather than inlined as a data URI so the shop can
#: replace the file without anyone editing HTML. It sits next to the two pages
#: that use it, and like them it is public -- the login page shows it before
#: anybody has a session, and a logo is not customer data.
LOGO_FILE = _DIR / "wanas.webp"

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


#: Everything from "conversations" down is one section of the dashboard and
#: one permission: it all reads or writes a customer's own messages. Login
#: (`/api/login`, `/api/me`, `/api/logout`) is deliberately *not* behind it --
#: an account with no permissions at all still has to be able to log in and be
#: told what it can reach.
INBOX_PERMISSION = "inbox"


def _inbox_guard(db, token: str | None):
    """`(staff, None)` when this account may work the inbox, `(None, response)`
    when it may not -- 401 with no session, 403 with one that lacks the
    permission. See `dashboard/guard.py`."""
    return require_permission(db, token, INBOX_PERMISSION)


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


def client_directory(db) -> dict[tuple[str, str], Client]:
    """Every conversation that belongs to a known customer, keyed by
    `(channel, external_id)`.

    Two queries for the whole page, never one per conversation. A link exists
    only once the customer has confirmed it or placed an order
    (`domain/services/identities.py`), so a conversation missing from this map
    is a person nobody has a name for yet -- not an error.
    """
    links = {
        (i.channel, i.external_id): i.client_id
        for i in db.scalars(select(ChannelIdentity)).all()
        if i.client_id is not None
    }
    if not links:
        return {}
    clients = {c.client_id: c for c in db.scalars(select(Client)).all()}
    return {key: clients[cid] for key, cid in links.items() if cid in clients}


#: Stands in for a conversation that has no identity row yet, so the label
#: helpers can ask for `.username` without a branch at every call site.
_NO_IDENTITY = ChannelIdentity(channel="", external_id="")


def handle_directory(db) -> dict[tuple[str, str], str]:
    """Every conversation whose platform handle we know, keyed the same way.

    Separate from `client_directory` because it answers a different question
    and is populated by a different thing: a `Client` exists once somebody
    has ordered or confirmed a link, while a handle is read off Meta the
    first time a person writes. Most Instagram conversations have the second
    and not the first, which is exactly the case this exists for.
    """
    return {
        (i.channel, i.external_id): i.username
        for i in db.scalars(select(ChannelIdentity)).all()
        if i.username
    }


#: The one channel whose `external_id` can itself be a phone number. On
#: Instagram it is an IGSID -- all digits, and `is_phone_number` says yes to
#: it, which is the whole reason this is decided by channel rather than by
#: looking at the string.
PHONE_CHANNELS = ("whatsapp",)


def customer_labels(
    client: Client | None,
    channel: str,
    external_id: str,
    handle: str | None = None,
) -> dict:
    """What to call a conversation, decided once and server-side.

    In order: the customer's own name, then their platform handle, then their
    phone number, then the id the channel handed us.

    The handle sits above the phone and below the name on purpose.
    `Client.full_name` is a name a person typed onto an order, so it wins
    everywhere it exists; an Instagram @handle is what that person calls
    themselves in public and is the only human-readable thing most Instagram
    conversations have, since the `external_id` there is an IGSID --
    seventeen digits, and `is_phone_number` says yes to them, which is why
    `PHONE_CHANNELS` decides that question by channel and not by looking at
    the string. Before this, a screenful of Instagram conversations was a
    screenful of numbers nobody could act on.

    `display_name` is the one the UI titles with, so the inbox list, the open
    thread, the dashboard's attention card and the busiest-conversations table
    cannot drift apart -- the thread header used to work it out for itself
    from the list it happened to have loaded, and fell back to the raw id
    whenever the conversation was opened from anywhere else.
    """
    name = ((client.full_name if client else "") or "").strip()
    phone = ((client.phone if client else "") or "").strip()
    if not phone and channel in PHONE_CHANNELS and is_phone_number(external_id):
        phone = external_id
    at = (handle or "").strip().lstrip("@")
    # Shown with the "@" the customer would recognise, stored without it.
    at_label = f"@{at}" if at else ""
    return {
        "customer_name": name or None,
        "customer_phone": phone or None,
        "customer_handle": at_label or None,
        "display_name": name or at_label or phone or external_id,
    }


def _conversation_summary(
    row: SessionRow, *, paused: bool, handoff, client: Client | None = None, handle: str | None = None
) -> dict:
    return {
        **customer_labels(client, row.channel, row.external_id, handle),
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


@router.get("/logo.webp")
def logo():
    """Public, like the login page that shows it. Cached hard: the file only
    changes when the shop rebrands, and a logo re-fetched on every navigation
    is the kind of thing nobody notices until the dashboard feels slow."""
    if not LOGO_FILE.exists():
        return JSONResponse({"error": "not_found"}, status_code=404)
    return FileResponse(
        LOGO_FILE,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"},
    )


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
        # `permissions` is what the sidebar hides nav items by. It is not what
        # protects anything -- every route it points at checks for itself
        # (`dashboard/guard.py::require_permission`).
        return JSONResponse(
            {
                "username": staff.username,
                "role": staff.role or staff_admin.OWNER_ROLE,
                "permissions": list(staff_admin.permission_keys(staff)),
            }
        )


# --------------------------------------------------------------------------
# conversations
# --------------------------------------------------------------------------


@router.get("/api/conversations")
def conversations(wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        _, refused = _inbox_guard(db, wanas_staff)
        if refused is not None:
            return refused

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

        directory = client_directory(db)
        handles = handle_directory(db)
        items = [
            _conversation_summary(
                row,
                paused=(row.channel, row.external_id) in paused_keys,
                handoff=handoffs.get((row.channel, row.external_id)),
                client=directory.get((row.channel, row.external_id)),
                handle=handles.get((row.channel, row.external_id)),
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
        _, refused = _inbox_guard(db, wanas_staff)
        if refused is not None:
            return refused

        # `transcript`, not `load`: reading a conversation must not be what
        # ends it. `load` moves the context bookmark on an idle session, which
        # is the agent's business and never a staff member's.
        history = session_store.transcript(db, channel, external_id)
        handoff = _open_handoffs(db).get((channel, external_id))
        paused = identities.is_paused(db, channel, external_id)

        return JSONResponse(
            {
                **customer_labels(
                    identities.client_for(db, channel, external_id),
                    channel,
                    external_id,
                    # Read off the identity rather than the list this thread
                    # was opened from, so a conversation reached by URL is
                    # titled the same as one clicked in the inbox.
                    (identities.get(db, channel, external_id) or _NO_IDENTITY).username,
                ),
                "channel": channel,
                "external_id": external_id,
                "history": display_history(history),
                # Whether a "seen by the customer" state means anything on
                # this channel at all. False hides the indicator rather than
                # showing every message as unread -- see
                # `assistant/display.py::RECEIPT_CHANNELS`.
                "receipts": supports_receipts(channel),
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
        _, refused = _inbox_guard(db, wanas_staff)
        if refused is not None:
            return refused
        identities.pause(db, channel, external_id)
        # A takeover is a person's decision, so it outranks the bot's own
        # recovery: clear the stamp that would let the customer's next message
        # pull the conversation back out from under whoever just claimed it.
        # See `assistant/recovery.py`.
        handoff = _open_handoffs(db).get((channel, external_id))
        if handoff is not None and (handoff.payload or {}).get("auto_resume_after_abandonment"):
            payload = dict(handoff.payload or {})
            payload.pop("auto_resume_after_abandonment", None)
            handoff.payload = payload
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
        staff, refused = _inbox_guard(db, wanas_staff)
        if refused is not None:
            return refused

        handoff = _open_handoffs(db).get((channel, external_id))
        if not identities.is_paused(db, channel, external_id):
            # Only a paused conversation is staff's to answer -- otherwise a
            # reply typed here lands in the middle of a turn the bot is still
            # running, which is exactly the two-writers race the debounce
            # lock exists to prevent.
            return JSONResponse({"error": "not_paused"}, status_code=409)

        if not notifications.window_open(db, channel, external_id):
            # Meta refuses free-form business-initiated text more than 24
            # hours after the customer's last message, and a template is not
            # something a staff member can type here. Said plainly, before
            # the send: the alternative is a 502 with a Meta error code in
            # it, and a staff member who believes the customer was answered.
            return JSONResponse({"error": "outside_window"}, status_code=409)

        sent = notifications.get_sender(channel).send_text(external_id, text)
        if not sent.delivered:
            return JSONResponse({"error": "send_failed", "detail": sent.error}, status_code=502)

        session_store.append(db, channel, external_id, msg.assistant(text, by="staff"))
        if handoff is not None:
            # The thing that needed attention has been answered; the queue
            # item's job is done.
            queues.resolve(db, handoff.queue_id, staff.staff_id)

        # The conversation stays paused. Replying is NOT releasing.
        #
        # This flipped back and forth once, so both halves are written down.
        # Auto-releasing on reply was meant to stop a forgotten pause from
        # silencing a number forever (the WhatsApp silence investigation).
        # What it actually produced is worse and much
        # harder to see: a staff member types an answer, the bot is live again
        # the instant they hit send, and the customer's next message -- the
        # reply to the sentence a *person* just wrote -- is answered by the
        # model, mid-handover, in the same thread. Two voices, one
        # conversation. A takeover that ends on its own is not a takeover.
        #
        # So control is now held until it is handed back explicitly, by
        # `/release` ("رجّع البوت" in the UI). The forgotten-pause risk is
        # covered where it belongs instead: the thread header and the inbox
        # both show a paused conversation as paused, `handle_message` logs
        # every dropped inbound with how long it has been waiting
        # (`_paused_note`), and `python manage.py release-conversation` is
        # the escape hatch of last resort.

    return JSONResponse({"ok": True})


@router.post("/api/conversations/{channel}/{external_id}/release")
def release(channel: str, external_id: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """Hand control back to the bot -- whichever way staff took it: a
    handoff (resolves the queue item too, for the "false alarm, no reply
    needed" case) or a manual takeover (nothing else to clear). No message
    goes out; the next thing the customer sends is what the bot answers."""
    with session_scope() as db:
        staff, refused = _inbox_guard(db, wanas_staff)
        if refused is not None:
            return refused

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
    `domain/services/conversation_reset.py` for exactly what is, and is
    not, touched: a real `Client` record and its order history are never
    reachable from here."""
    with session_scope() as db:
        staff, refused = _inbox_guard(db, wanas_staff)
        if refused is not None:
            return refused
        conversation_reset.reset(db, channel, external_id, staff_id=staff.staff_id)
    return JSONResponse({"ok": True})


@router.get("/media")
def media(path: str = Query(...), wanas_staff: str | None = Cookie(default=None)):
    """The dashboard's own copy of the harness's `/media`, because the harness
    is off in production (`HARNESS_ENABLED=0`) and the dashboard has to work
    without it. Only local catalog files ever come through here -- a Shopify
    photo is already an `http(s)` url the browser loads directly; see
    `_is_url` in `integrations/whatsapp/client.py`."""
    with session_scope() as db:
        _, refused = _inbox_guard(db, wanas_staff)
        if refused is not None:
            return refused

    target = resolve_servable_path(path)
    if target is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return FileResponse(target)
