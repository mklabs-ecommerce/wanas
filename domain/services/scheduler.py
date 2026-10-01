"""The one place anything in this app runs on a clock rather than a request.

The back-in-stock check (`domain/services/reengagement.py`) and the
"customer went silent" nudges (`assistant/silence_nudges.py`, registered with
`register_nudge_job` since domain/ never imports assistant/) are polls: a
variant coming back in stock and a customer going quiet are both things
nothing tells this app about, so something has to periodically ask. The
nudges run on their own, finer clock (`NUDGE_POLL_SECONDS`) -- a 10-minute
nudge polled every 30 minutes would be a 40-minute one. The catalogue
check is a poll for a different reason -- Shopify *does* tell us, on
`products/create`, but that delivery is refused unless
`SHOPIFY_WEBHOOK_SECRET` is set, and a product staff added being invisible to
every customer is too expensive a thing to leave resting on one environment
variable. The webhook is the fast path; this is the floor under it. In-process and
single-instance, the same scope note as `assistant/dispatcher.py`: this fits
one Railway instance. Two instances would each run their own copy of this
loop -- both jobs are idempotent against a duplicate pass (the waitlist
entry's `notified_at`; the nudge row's conditional-UPDATE claim), so that
degrades to "maybe checked twice in the same minute," never a double message.
Moving this to a real cron means replacing this file, not its callers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from config.settings import settings
from domain.db import session_scope
from domain.services import reengagement
from integrations.instagram import token as instagram_token

log = logging.getLogger("rehla.scheduler")

_nudge_job: Callable[[], int] | None = None


def register_nudge_job(job: Callable[[], int]) -> None:
    """Called once at startup with `assistant.silence_nudges.check_silences`."""
    global _nudge_job
    _nudge_job = job


class Scheduler:
    def __init__(self, interval_seconds: float | None = None):
        self._interval = (
            settings.reengagement_interval_seconds if interval_seconds is None else interval_seconds
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._interval <= 0:
            log.info("re-engagement scheduler disabled (REENGAGEMENT_INTERVAL_SECONDS <= 0)")
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rehla-scheduler", daemon=True)
        self._thread.start()
        log.info("re-engagement scheduler started (every %.0fs)", self._interval)

    def _run(self) -> None:
        # Wait first: a fresh boot has nothing new to find, and running the
        # checks before the rest of startup (Shopify import, webhook
        # registration) has settled just adds noise to the same log burst.
        poll = settings.nudge_poll_seconds
        step = min(self._interval, poll) if poll > 0 else self._interval
        next_slow = time.monotonic() + self._interval
        while not self._stop.wait(step):
            if poll > 0:
                self._nudge_tick()
            if time.monotonic() >= next_slow:
                next_slow = time.monotonic() + self._interval
                self._tick()

    def _nudge_tick(self) -> None:
        if _nudge_job is None:
            return
        try:
            sent = _nudge_job()
            if sent:
                log.info("silence nudges: sent %d", sent)
        except Exception:
            log.exception("silence nudge check failed")

    def _tick(self) -> None:
        try:
            notified = reengagement.check_back_in_stock()
            if notified:
                log.info("back-in-stock: notified %d waitlist entrie(s)", notified)
        except Exception:
            log.exception("back-in-stock check failed")
        try:
            # Rate-limited internally to one attempt per day; cheap no-op on
            # every other tick. This is what stops the Instagram channel from
            # dying silently 60 days after launch.
            instagram_token.scheduled_refresh()
        except Exception:
            log.exception("instagram token refresh failed")
        try:
            self._import_new_products()
        except Exception:
            log.exception("catalog import failed")

    def _import_new_products(self) -> None:
        """Mirror any product added in Shopify Admin since the last tick.

        Costs one list read when nothing has changed: `product_import` skips
        the per-product detail call for anything whose SKUs it already knows
        (`_product_summary` carries them, and the list query was fetching them
        anyway). Only a genuinely new product costs more than that.

        Deliberately silent on a quiet tick -- a log line every half hour
        saying "nothing happened" is how a log stops being read.
        """
        if not settings.shopify_configured:
            return
        from integrations.shopify.product_import import import_missing_products

        with session_scope() as db:
            report = import_missing_products(db, apply=True)
        for entry in report["imported"]:
            log.info(
                "catalog import: %r is now in the catalog as %s (%d variant(s))",
                entry["title"],
                entry["product_id"],
                entry["variants"],
            )
        for problem in report["problems"]:
            log.warning("catalog import: %s", problem)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


#: One per process, same shape as `assistant.channels.whatsapp.dispatcher`.
scheduler = Scheduler()
