"""Work the customer's answer does not depend on, moved out of its way.

Three Meta round trips sit between a message arriving and the debounce window
opening: the blue ticks, the typing bubble, and (once per customer) the handle
lookup. Measured from the Railway container, a Graph call is 196 ms on
WhatsApp and 135 ms on Instagram, so an Instagram message was paying about
400 ms before the process had even started thinking about it -- and not one of
those calls has an answer anything waits for. The same is true of a Shopify
read started early so the first model hop pays for it instead of the tool that
needs it.

So they run here. The rules this follows are the ones that make "fire and
forget" safe rather than a source of invisible failures:

* **A bounded pool, not a thread per call.** Unbounded thread creation keyed on
  inbound message volume is a failure mode that only shows up on the day the
  shop gets busy. The queue is unbounded and the workers are few: these are
  short network calls, and a backlog of read receipts is not worth a thread
  each.
* **Nothing raises out of here.** A failure is logged and dropped. The whole
  premise is that the work does not matter enough to block on; it therefore
  does not matter enough to break a turn either.
* **Daemon threads, and a flush on shutdown.** A deploy must not hang waiting
  for a typing indicator, and must not silently lose the ones already queued.
* **Never for anything the reply's correctness depends on.** Recording the
  inbound message, claiming the message id, and every write that decides what
  to say stay exactly where they are, in the request, in order. The test for
  whether something belongs here is whether the customer would notice it
  never happened -- a read receipt, no; a stored message, yes.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("wanas.offthread")

#: Small on purpose. Everything submitted here is a short HTTP call, and the
#: point is to get them off one thread, not to run hundreds at once against an
#: API with its own rate limits.
_WORKERS = 4

_pool = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="wanas-bg")


def run_later(what: str, fn: Callable, *args, **kwargs) -> None:
    """Run `fn` on a background thread. Never raises, never blocks.

    `what` names the work for the log line a failure produces -- "read
    receipt", "typing indicator" -- because a traceback from a pool thread
    with no context is the hardest kind of log entry to act on.
    """

    def _guarded() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            log.warning("background %s failed", what, exc_info=True)

    try:
        _pool.submit(_guarded)
    except RuntimeError:
        # The pool is shutting down (a deploy). Do it inline rather than drop
        # it: this is the last thing that will happen either way.
        _guarded()


@atexit.register
def _shutdown() -> None:  # pragma: no cover - process teardown
    _pool.shutdown(wait=False)
