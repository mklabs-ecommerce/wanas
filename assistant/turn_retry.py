"""Retrying a whole turn once, silently, before falling back to a person.

`assistant/agent.py::run_turn` already retries the one call inside it that is
most likely to fail transiently -- the model call itself (see
`_generate_with_retry` there). This module is the wider net underneath that:
the channel adapters' `_deliver` wraps the *entire* `handle_message(...)` call
in it, for whatever crashes **outside** `run_turn`'s own guard -- session or
identity plumbing, media handling, a Shopify read, or any bug nothing above
this classified. That escape hatch used to go straight to the crash fallback
(a generic apology plus a staff alert) on the very first failure, which is
right for a real bug but wrong for the kind of one-off hiccup -- a dropped
connection, a database momentarily unreachable -- that a second attempt
thirty seconds later would simply not hit again.

**Why a whole-turn retry here is safe to just try again.** `handle_message`
runs inside `domain/db.py::session_scope()` when called with no `db` (which is
how every channel adapter calls it): a failure anywhere inside rolls the
*entire* transaction back, so a second attempt starts from exactly the state
the first one did -- the message `record_inbound` already committed at
ingest, and nothing else. It cannot double up the transcript, double-charge a
cart, or place two orders; the same guarantee that makes a platform's own
retry-on-timeout safe to just let happen (`assistant/runtime.py::claim_message`
/ `release_claims`) makes one retry from here safe too.

**Why this is one retry and not a loop.** A second failure means either the
same transient condition is still there -- worth telling staff about, not
worth silently retrying forever and leaving the customer with nothing longer
still -- or the failure was never transient at all. Either way the existing
crash-fallback path (release the claimed message ids so a platform retry is
processed, alert staff, send the generic apology) is exactly right, and this
module changes nothing about it -- it only delays reaching it by one
thirty-second attempt.

**What this does not cover.** A `request_human` handoff -- even a wrongly
triggered one -- is not a crash: `run_turn` returned normally, a real message
was queued and the conversation paused on purpose. Retrying that on a timer
would mean the bot second-guessing a customer who explicitly asked for a
person, or re-asking the same unclear question and getting the same answer
since nothing about the conversation changed. That case is handled by
`assistant/recovery.py` instead, which only acts once the *customer* has said
something new.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger("wanas.turn_retry")

#: How long to wait before the one retry. Long enough that a rate limit or a
#: dropped connection has had a real chance to clear; short enough that the
#: customer is not left with dead air for minutes -- Meta's own webhook
#: delivery has already moved on by the time this fires, so nothing about the
#: platform side is racing this.
RETRY_DELAY_SECONDS = 30.0

#: Seam for tests: replaced with a no-op so a retry can be proven to happen
#: without a suite actually sleeping thirty seconds.
_sleep = time.sleep

T = TypeVar("T")


def call_with_retry(fn: Callable[[], T], *, channel: str, external_id: str) -> T:
    """Run `fn()`; on any exception, wait `RETRY_DELAY_SECONDS` and run it
    once more. The second failure (if there is one) propagates to the caller
    unchanged -- this never swallows an error, it only delays it by one
    attempt.
    """
    try:
        return fn()
    except Exception:
        log.warning(
            "turn crashed for %s/%s outside the model call; waiting %.0fs and "
            "retrying the whole turn once before falling back",
            channel,
            external_id,
            RETRY_DELAY_SECONDS,
            exc_info=True,
        )
    _sleep(RETRY_DELAY_SECONDS)
    return fn()
