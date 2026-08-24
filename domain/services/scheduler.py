"""The one place anything in this app runs on a clock rather than a request.

Both re-engagement checks (`backend/services/reengagement.py`) are polls: a
variant coming back in stock and a cart going idle are both things nothing
tells this app about, so something has to periodically ask. In-process and
single-instance, the same scope note as `assistant/dispatcher.py`: this fits
one Railway instance. Two instances would each run their own copy of this
loop -- both jobs are idempotent against a duplicate pass (the waitlist
entry's `notified_at`, the nudge row's `sent_at`), so that degrades to "maybe
checked twice in the same minute," never a double message or a missed one.
Moving this to a real cron means replacing this file, not its callers.
"""

from __future__ import annotations

import logging
import threading

from config.settings import settings
from domain.services import reengagement
from integrations.instagram import token as instagram_token

log = logging.getLogger("wanas.scheduler")


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
        self._thread = threading.Thread(target=self._run, name="wanas-scheduler", daemon=True)
        self._thread.start()
        log.info("re-engagement scheduler started (every %.0fs)", self._interval)

    def _run(self) -> None:
        # Wait first: a fresh boot has nothing new to find, and running the
        # checks before the rest of startup (Shopify import, webhook
        # registration) has settled just adds noise to the same log burst.
        while not self._stop.wait(self._interval):
            self._tick()

    def _tick(self) -> None:
        try:
            notified = reengagement.check_back_in_stock()
            if notified:
                log.info("back-in-stock: notified %d waitlist entrie(s)", notified)
        except Exception:
            log.exception("back-in-stock check failed")
        try:
            nudged = reengagement.check_abandoned_carts()
            if nudged:
                log.info("abandoned-cart: nudged %d conversation(s)", nudged)
        except Exception:
            log.exception("abandoned-cart check failed")
        try:
            # Rate-limited internally to one attempt per day; cheap no-op on
            # every other tick. This is what stops the Instagram channel from
            # dying silently 60 days after launch.
            instagram_token.scheduled_refresh()
        except Exception:
            log.exception("instagram token refresh failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


#: One per process, same shape as `assistant.channels.whatsapp.dispatcher`.
scheduler = Scheduler()
