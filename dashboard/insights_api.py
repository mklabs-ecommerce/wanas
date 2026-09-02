"""Messaging insights: what the *conversations* look like, not what the shop sold.

`stats_api.py` answers commerce questions from Shopify. This answers the
other half of the business -- how many people the bot is talking to, on which
channel, how often it has to hand a conversation to a person, which tools it
actually reaches for, and how many Instagram comments turned into a DM. All
of it comes from Postgres, because none of it exists in Shopify.

One honest limitation, stated here rather than papered over: **stored
messages carry no per-message timestamp** (`assistant/messages.py` -- the
history is the provider-neutral message list, and adding a field to it would
change the contract every provider translates). So a "messages per day"
series is not derivable, and this module does not invent one. What is
timestamped is real and is what gets charted: a session's `updated_at` (the
day a conversation was last active), an identity's `first_seen_at` (a new
contact), a queue item's `created_at` (a handoff or an alert), and an
Instagram comment reply's `created_at`. Volume is reported as a total per
conversation, which is exactly as precise as the data actually is.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Cookie, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select

from common.timeutil import as_aware
from dashboard import ranges
from dashboard.guard import require_permission
from dashboard.web import client_directory, customer_labels, handle_directory
from domain.db import session_scope
from domain.models import (
    ChannelIdentity,
    InstagramCommentReply,
    QueueKind,
    QueueStatus,
    SessionRow,
    StaffQueueItem,
)

router = APIRouter(prefix="/dashboard/api/insights", tags=["dashboard-insights"])

#: The same window the Statistics tab uses, parsed by the same code
#: (`dashboard/ranges.py`) -- two tabs of one page answering about different
#: fortnights is the failure that shares this module.
ALLOWED_RANGES = ranges.ALLOWED_RANGES

#: How many distinct tool names to report.
TOP_TOOLS = 12


def _day(value) -> str | None:
    aware = as_aware(value)
    return aware.date().isoformat() if aware else None


def _series(counter: dict[str, int], window: ranges.Window) -> list[dict]:
    """Every day in the window, zero-filled, in order.

    Built from the window's own dates, never from "the last N days counting
    back from today". A line chart with holes in it reads as "no data" where
    the truth is "no activity"; a chart anchored on today reads as "no
    activity" for a historical range whose every point was real, because each
    of those dates falls outside the keys that were pre-filled.
    """
    filled = dict.fromkeys(window.each_day(), 0)
    for day, count in counter.items():
        if day in filled:
            filled[day] = count
    return [{"date": day, "count": count} for day, count in filled.items()]


def _conversation_facts(history: list) -> dict:
    """Everything countable in one stored conversation, in a single pass."""
    if not isinstance(history, list):
        # `LenientJSON` hands back a sentinel for a row that failed to decode
        # rather than raising mid-query; it is one broken conversation, not a
        # reason for the whole page to 500.
        return {"customer": 0, "bot": 0, "staff": 0, "tools": Counter(), "refusals": 0, "media": 0}

    customer = bot = staff = refusals = media = 0
    tools: Counter[str] = Counter()
    for message in history:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            customer += 1
            if message.get("images") or message.get("audio"):
                media += 1
        elif role == "assistant":
            if message.get("content"):
                if message.get("by") == "staff":
                    staff += 1
                else:
                    bot += 1
            for call in message.get("tool_calls") or []:
                name = (call or {}).get("name")
                if name:
                    tools[name] += 1
        elif role == "tool_results":
            for result in message.get("results") or []:
                if ((result or {}).get("content") or {}).get("error"):
                    refusals += 1
    return {
        "customer": customer,
        "bot": bot,
        "staff": staff,
        "tools": tools,
        "refusals": refusals,
        "media": media,
    }


@router.get("")
def insights(
    days: int | None = Query(default=None, description="one of the presets: 7, 30, 90"),
    start: str | None = Query(default=None, description="custom range start, YYYY-MM-DD (with end)"),
    end: str | None = Query(default=None, description="custom range end, YYYY-MM-DD (inclusive)"),
    wanas_staff: str | None = Cookie(default=None),
) -> JSONResponse:
    try:
        window = ranges.parse(days=days, start=start, end=end)
    except ranges.BadRange as exc:
        return JSONResponse({"error": "bad_arguments", "detail": str(exc)}, status_code=400)

    # Both bounds, always. Every filter below used to be `>= since` alone,
    # which is correct only while the window ends today -- a custom range
    # would have swept in everything after its end date and reported it as
    # part of the fortnight on screen.
    since, until = window.bounds()

    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, "analytics")
        if refused is not None:
            return refused

        rows = db.scalars(select(SessionRow)).all()

        active_by_day: Counter[str] = Counter()
        by_channel: Counter[str] = Counter()
        tool_totals: Counter[str] = Counter()
        totals = Counter()
        busiest: list[dict] = []
        # Named the same way the inbox names them -- a table of raw ids is a
        # table nobody can act on.
        directory = client_directory(db)
        handles = handle_directory(db)

        for row in rows:
            updated = as_aware(row.updated_at)
            if updated is None or updated < since or updated > until:
                continue
            facts = _conversation_facts(row.history or [])
            day = _day(row.updated_at)
            if day:
                active_by_day[day] += 1
            by_channel[row.channel] += 1
            tool_totals.update(facts["tools"])
            totals["conversations"] += 1
            totals["customer_messages"] += facts["customer"]
            totals["bot_messages"] += facts["bot"]
            totals["staff_messages"] += facts["staff"]
            totals["refusals"] += facts["refusals"]
            totals["media_messages"] += facts["media"]
            busiest.append(
                {
                    **customer_labels(
                        directory.get((row.channel, row.external_id)),
                        row.channel,
                        row.external_id,
                        handles.get((row.channel, row.external_id)),
                    ),
                    "channel": row.channel,
                    "external_id": row.external_id,
                    "messages": facts["customer"] + facts["bot"] + facts["staff"],
                    "customer_messages": facts["customer"],
                    "staff_messages": facts["staff"],
                    "updated_at": updated.isoformat(),
                }
            )

        new_contacts: Counter[str] = Counter()
        identity_rows = db.scalars(
            select(ChannelIdentity).where(
                ChannelIdentity.first_seen_at >= since, ChannelIdentity.first_seen_at <= until
            )
        ).all()
        for identity in identity_rows:
            day = _day(identity.first_seen_at)
            if day:
                new_contacts[day] += 1

        paused_count = len(
            db.scalars(
                select(ChannelIdentity.external_id).where(
                    ChannelIdentity.paused_until_staff_reply.is_(True)
                )
            ).all()
        )

        queue_rows = db.scalars(
            select(StaffQueueItem).where(
                StaffQueueItem.created_at >= since, StaffQueueItem.created_at <= until
            )
        ).all()
        handoffs_by_day: Counter[str] = Counter()
        queue_by_kind: Counter[str] = Counter()
        queue_by_reason: Counter[str] = Counter()
        resolution_minutes: list[float] = []
        open_now = 0
        for item in queue_rows:
            queue_by_kind[item.kind] += 1
            if item.reason:
                queue_by_reason[item.reason] += 1
            if item.kind == QueueKind.HANDOFF.value:
                day = _day(item.created_at)
                if day:
                    handoffs_by_day[day] += 1
            if item.status == QueueStatus.OPEN.value:
                open_now += 1
            created, resolved = as_aware(item.created_at), as_aware(item.resolved_at)
            if created and resolved and resolved >= created:
                resolution_minutes.append((resolved - created).total_seconds() / 60)

        comment_rows = db.scalars(
            select(InstagramCommentReply).where(
                InstagramCommentReply.created_at >= since,
                InstagramCommentReply.created_at <= until,
            )
        ).all()
        comments_by_day: Counter[str] = Counter()
        comment_public = comment_private = 0
        for comment in comment_rows:
            day = _day(comment.created_at)
            if day:
                comments_by_day[day] += 1
            comment_public += 1 if comment.public_replied else 0
            comment_private += 1 if comment.private_replied else 0

    busiest.sort(key=lambda c: c["messages"], reverse=True)
    conversations = totals["conversations"]
    handoff_total = queue_by_kind.get(QueueKind.HANDOFF.value, 0)
    median_resolution = None
    if resolution_minutes:
        ordered = sorted(resolution_minutes)
        middle = len(ordered) // 2
        median_resolution = round(
            ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2,
            1,
        )

    return JSONResponse(
        {
            **window.as_payload(),
            "totals": {
                "conversations": conversations,
                "customer_messages": totals["customer_messages"],
                "bot_messages": totals["bot_messages"],
                "staff_messages": totals["staff_messages"],
                "media_messages": totals["media_messages"],
                "refusals": totals["refusals"],
                "new_contacts": sum(new_contacts.values()),
                "paused_now": paused_count,
                "queue_open_now": open_now,
                "handoffs": handoff_total,
                # The share of conversations a person had to step into. The
                # single most useful number on this page: it is what "is the
                # bot actually coping" looks like as a figure.
                "handoff_rate": round(handoff_total / conversations, 3) if conversations else 0.0,
                "autonomy_rate": (
                    round(1 - (handoff_total / conversations), 3) if conversations else 0.0
                ),
                "median_resolution_minutes": median_resolution,
                "instagram_comments": len(comment_rows),
                "instagram_comment_public_replies": comment_public,
                "instagram_comment_private_replies": comment_private,
            },
            "active_conversations_by_day": _series(active_by_day, window),
            "new_contacts_by_day": _series(new_contacts, window),
            "handoffs_by_day": _series(handoffs_by_day, window),
            "instagram_comments_by_day": _series(comments_by_day, window),
            "by_channel": dict(by_channel),
            "queue_by_kind": dict(queue_by_kind),
            "queue_by_reason": dict(queue_by_reason.most_common(10)),
            "top_tools": [
                {"name": name, "count": count} for name, count in tool_totals.most_common(TOP_TOOLS)
            ],
            "busiest_conversations": busiest[:10],
            # Stated in the payload, not only in this docstring, so the page
            # can say it on screen instead of implying a precision it lacks.
            "note": (
                "per-message timestamps are not stored; daily series count "
                "conversation activity, not messages"
            ),
        }
    )
