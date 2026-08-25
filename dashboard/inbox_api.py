"""The unified inbox: WhatsApp, Instagram DMs and Instagram comments, one list.

`web.py`'s `/api/conversations` is the original, and it stays exactly as it
is -- it is what the handoff queue depends on and its ordering (paused first,
oldest wait first) is a promise about work order, not a display preference.
This adds the thing a person working the inbox all day needs on top of it and
that endpoint deliberately does not do: search across message text, filter by
channel and by who currently owns the conversation, per-filter counts, and the
Instagram comment stream that has no `SessionRow` of its own at all.

Reads only. Every action -- takeover, reply, release, reset -- still goes
through `web.py`, so there is exactly one place where a message can leave
this dashboard and exactly one guard on it.

Comments are a separate stream on purpose. A comment is not a conversation:
`InstagramCommentReply` is a one-row-per-comment ledger written *before* the
private reply is sent (see its model docstring -- that ordering is what makes
"one private reply per comment, ever" survive a crash), so what it can show
is which comments were seen and how they were answered, never a thread.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select

from common.timeutil import as_aware
from dashboard.guard import staff_for, unauthenticated
from dashboard.web import _conversation_summary, _open_handoffs, _paused_identity_keys
from domain.db import session_scope
from domain.models import ChannelIdentity, Client, InstagramCommentReply, SessionRow

router = APIRouter(prefix="/dashboard/api/inbox", tags=["dashboard-inbox"])

#: Conversations returned in one response, after filtering. The same order of
#: magnitude as `web.py`'s `MAX_CONVERSATIONS`, for the same reason: this shop
#: does not have more, and a cap means one huge history table cannot make the
#: page unusable if it ever does.
MAX_RESULTS = 300

#: How deep into one conversation a text search looks, in messages, counting
#: back from the most recent. Searching an entire multi-year history for every
#: conversation on every keystroke is what makes a search box feel broken.
SEARCH_DEPTH = 60

STATUSES = ("all", "needs_reply", "paused", "bot", "unanswered")


def _searchable(history: list, needle: str) -> bool:
    if not isinstance(history, list):
        return False
    for message in reversed(history[-SEARCH_DEPTH:]):
        if not isinstance(message, dict):
            continue
        if needle in (message.get("content") or "").lower():
            return True
    return False


def _last_role(history: list) -> str | None:
    """Who spoke last. A conversation whose last word is the customer's is
    the one nobody has answered -- the `unanswered` filter is built on this
    and not on a read/unread flag, because nothing in this system has ever
    recorded a staff member reading anything.

    An automated push (`by="system"`: an order confirmation, a shipping
    update, a cart nudge) is skipped rather than counted as an answer. It goes
    out on a clock or on a Shopify webhook, knowing nothing about the question
    the customer asked -- letting one stand as the last word would quietly
    empty this filter of exactly the conversations it exists to surface."""
    if not isinstance(history, list):
        return None
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            return "customer"
        if role == "assistant" and message.get("content"):
            by = message.get("by")
            if by == "system":
                continue
            return "staff" if by == "staff" else "bot"
    return None


def _message_count(history: list) -> int:
    if not isinstance(history, list):
        return 0
    return sum(
        1
        for m in history
        if isinstance(m, dict)
        and (m.get("role") == "user" or (m.get("role") == "assistant" and m.get("content")))
    )


@router.get("")
def inbox(
    q: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    status: str = Query(default="all"),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    if status not in STATUSES:
        detail = f"status must be one of {STATUSES}"
        return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)

    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

        handoffs = _open_handoffs(db)
        paused_keys = _paused_identity_keys(db)
        rows = list(db.scalars(select(SessionRow).order_by(SessionRow.updated_at.desc())).all())

        # A conversation belonging to a known customer shows their name, not
        # a bare phone number. Two queries, not one per row.
        identities = {
            (i.channel, i.external_id): i.client_id
            for i in db.scalars(select(ChannelIdentity)).all()
            if i.client_id is not None
        }
        clients = {c.client_id: c for c in db.scalars(select(Client)).all()} if identities else {}

        needle = (q or "").strip().lower()
        items: list[dict] = []
        for row in rows:
            key = (row.channel, row.external_id)
            history = row.history or []
            client = clients.get(identities.get(key)) if identities else None

            if needle:
                hit = (
                    needle in row.external_id.lower()
                    or (client is not None and needle in (client.full_name or "").lower())
                    or _searchable(history, needle)
                )
                if not hit:
                    continue

            summary = _conversation_summary(
                row, paused=key in paused_keys, handoff=handoffs.get(key)
            )
            summary["last_role"] = _last_role(history)
            summary["message_count"] = _message_count(history)
            summary["customer_name"] = client.full_name if client else None
            summary["client_id"] = client.client_id if client else None
            items.append(summary)

        counts = {
            "all": len(items),
            "needs_reply": sum(1 for c in items if c["paused"] and c["reason"] != "manual"),
            "paused": sum(1 for c in items if c["paused"]),
            "bot": sum(1 for c in items if not c["paused"]),
            "unanswered": sum(1 for c in items if c["last_role"] == "customer"),
        }
        by_channel: dict[str, int] = {}
        for item in items:
            by_channel[item["channel"]] = by_channel.get(item["channel"], 0) + 1

    if channel:
        items = [c for c in items if c["channel"] == channel]
    if status == "needs_reply":
        items = [c for c in items if c["paused"] and c["reason"] != "manual"]
    elif status == "paused":
        items = [c for c in items if c["paused"]]
    elif status == "bot":
        items = [c for c in items if not c["paused"]]
    elif status == "unanswered":
        items = [c for c in items if c["last_role"] == "customer"]

    # Same work order `web.py` promises: anyone waiting on a person comes
    # first, longest wait at the top, then everything else by recency.
    paused = sorted((c for c in items if c["paused"]), key=lambda c: c["waiting_since"] or "")
    rest = [c for c in items if not c["paused"]]
    ordered = (paused + rest)[:MAX_RESULTS]

    return JSONResponse(
        {
            "conversations": ordered,
            "counts": counts,
            "by_channel": by_channel,
            "shown_count": len(ordered),
        }
    )


@router.get("/comments")
def comments(
    days: int = Query(default=30, ge=1, le=365), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    """The Instagram comment ledger: what was handled, and how.

    `public_replied` and `private_replied` are both False for a comment that
    was seen and deliberately not engaged with -- a negative comment raises a
    silent internal alert instead (`assistant/channels/instagram.py`), and
    showing it here as "no reply" is the truth, not a missing row.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

        rows = db.scalars(
            select(InstagramCommentReply)
            .where(InstagramCommentReply.created_at >= since)
            .order_by(InstagramCommentReply.created_at.desc())
            .limit(MAX_RESULTS)
        ).all()

        items = [
            {
                "comment_id": row.comment_id,
                "media_id": row.media_id,
                "commenter_igsid": row.commenter_igsid,
                "public_replied": row.public_replied,
                "private_replied": row.private_replied,
                "created_at": (as_aware(row.created_at).isoformat() if row.created_at else None),
            }
            for row in rows
        ]

    return JSONResponse(
        {
            "comments": items,
            "range_days": days,
            "public_replies": sum(1 for i in items if i["public_replied"]),
            "private_replies": sum(1 for i in items if i["private_replied"]),
        }
    )
