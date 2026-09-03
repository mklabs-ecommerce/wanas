"""Taking a conversation back after the bot has walked out of it.

A handoff pauses a conversation until a staff member opens the dashboard
(`assistant/tools/support_tools.py::raise_handoff` ->
`domain/services/identities.py::pause`). That is right when a person is
genuinely needed. It is a dead end when the handoff was a false positive: the
customer writes again two minutes later, and every word of it is stored,
recorded and never answered.

This module is the safety net under that. It answers one question --
"may this conversation carry on by itself?" -- and the answer is yes only when
all of these hold:

* the newest handoff for the identity was raised over **a message the bot
  could not act on**, not over a person's decision (`RESUMABLE_REASONS`, and
  the stamp `raise_handoff` writes onto the payload for exactly those);
* it is still `open`, which is what says no staff member has replied -- the
  dashboard's reply route resolves the item as it sends;
* nobody has taken the conversation over by hand since (a `takeover` clears
  the stamp, and a staff line in the recent transcript stops it too);
* and the customer's new message arrived inside `RESUME_WINDOW` of the
  handoff. Ten minutes is short on purpose: it is the span in which the
  customer is still in the same conversation. An hour later they are not
  resuming, they are waiting on the person they were promised.

**There is no second history path here.** The resumed turn runs through
`agent.run_turn` exactly like any other, reading the same stored session and
the same `assistant/context.py` view of it -- the last messages verbatim, the
older ones compacted to what was said. That is the whole reason the recovery
is one flag and one appended prompt paragraph rather than a "replay the last
ten messages" routine of its own: a parallel context builder is how the
compaction bugs this codebase has already paid for got in. All this adds is
the instruction to *use* that context the right way -- read the recent
exchange as one piece, answer only the newest message in it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from assistant.tools.support_tools import RESUMABLE_REASONS
from common.timeutil import as_aware, utcnow
from domain.models import QueueKind, QueueStatus, StaffQueueItem

log = logging.getLogger("wanas.recovery")

#: How long after the bot leaves a conversation its own next message may take
#: it back. Roughly ten minutes: long enough that a customer who reads the
#: "someone will get back to you" line, thinks, and types again is still the
#: same conversation; short enough that it cannot pull a conversation out from
#: under a staff member who has it open but has not typed yet.
RESUME_WINDOW = timedelta(minutes=10)

#: Appended to the system prompt for the resumed turn, and nothing else about
#: the turn changes. It says the two things the model cannot work out from the
#: history alone: that the pause is over, and that the several messages it can
#: see are context for the newest one rather than a queue of things to answer.
RESUME_INSTRUCTION = """
# المحادثة دي رجعت لك
المحادثة دي وقفت من شوية والزبون بعت تاني دلوقتي، والرد رجع لك انت.
- اقرا آخر الرسايل اللي في المحادثة كلها مع بعض كسياق واحد — اللي كان بينكم قبل ما تقف وبعدها — وافهم منها المنتج واللون والمقاس والأوردر اللي كنتوا فيه.
- بعد ما تفهم السياق، ردّ **على آخر رسالة بس**: رد واحد يجاوب اللي الزبون قاله دلوقتي بالظبط، مش رد على رسالة قديمة ومش رسالة ترحيب من الأول.
- اتصرف كإنك مسبتش المحادثة أصلاً: متعتذرش عن التأخير، متقولش إنك كنت هتحوله لحد، ومتسألش عن حاجة هو قالها فوق.
""".strip()


def _newest_handoff(db: Session, channel: str, external_id: str) -> StaffQueueItem | None:
    return db.scalar(
        select(StaffQueueItem)
        .where(
            StaffQueueItem.channel == channel,
            StaffQueueItem.external_id == external_id,
            StaffQueueItem.kind == QueueKind.HANDOFF.value,
        )
        .order_by(StaffQueueItem.created_at.desc())
    )


def _staff_spoke_recently(history: list[dict], since) -> bool:
    """Has a person written into this conversation since the handoff?

    The dashboard's reply route resolves the handoff item as it sends, so the
    `open` check above catches the ordinary case on its own. This is the
    second lock, for a staff line that arrived by some other route: a message
    with no `at` stamp predates the feature and therefore predates a handoff
    raised minutes ago, so it is not one.
    """
    for message in history:
        if message.get("by") != "staff":
            continue
        at = message.get("at")
        if not at:
            continue
        try:
            when = as_aware(datetime.fromisoformat(at))
        except ValueError:  # pragma: no cover - a stamp we did not write
            continue
        if when >= since:
            return True
    return False


def resumable_handoff(
    db: Session, channel: str, external_id: str, history: list[dict] | None = None
) -> StaffQueueItem | None:
    """The handoff this conversation may take back, or None.

    Read-only. `take_back` is what actually acts on it.
    """
    item = _newest_handoff(db, channel, external_id)
    if item is None or item.created_at is None:
        # No handoff record at all means a manual takeover from the dashboard
        # (`dashboard/web.py::takeover`), which is a person's decision and not
        # ours to undo.
        return None
    if item.status != QueueStatus.OPEN.value:
        return None
    if item.reason not in RESUMABLE_REASONS:
        return None
    if not (item.payload or {}).get("auto_resume_after_abandonment"):
        # Either an older handoff raised before this shipped, or one a staff
        # takeover has since claimed. Both mean: leave it alone.
        return None

    raised_at = as_aware(item.created_at)
    if utcnow() - raised_at > RESUME_WINDOW:
        return None
    if history and _staff_spoke_recently(history, raised_at):
        return None
    return item


def take_back(db: Session, item: StaffQueueItem) -> None:
    """Clear the stamp so this abandonment is only ever resumed once.

    The queue item itself is deliberately left **open**. The bot leaving a
    conversation it should not have left is worth a person's eye whether or
    not it recovered, and resolving it here would be the bot marking its own
    false positive as handled. Staff see it, with `auto_resumed_at` on the
    payload saying the customer was not left waiting for them.
    """
    payload = dict(item.payload or {})
    payload.pop("auto_resume_after_abandonment", None)
    payload["auto_resumed_at"] = utcnow().isoformat()
    item.payload = payload
    db.flush()
    log.info(
        "resuming %s/%s after handoff %s (reason=%s): the customer wrote back inside %s",
        item.channel,
        item.external_id,
        item.queue_id,
        item.reason,
        RESUME_WINDOW,
    )
