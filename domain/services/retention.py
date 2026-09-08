"""Housekeeping for the tables nothing else ever deletes from.

One table so far, and it is the one that mattered: `webhook_events`.

A row is written there for every inbound WhatsApp message, every Instagram
message *and* comment, and every Shopify delivery -- one per platform id, as
the idempotency claim that stops a retry being processed twice
(`assistant/runtime.py::claim_message`,
`integrations/shopify/webhooks.py::_claim`). Nothing ever removed one except
`release_claims`, which only fires when a turn has already failed. So the
table was append-only for the life of the deployment: one row per message the
shop has *ever* received, on the same small Postgres that holds the orders.
That is the slow, invisible kind of problem -- nothing breaks, the database
simply never stops growing, and the day it is noticed is the day it is
already large.

The `received_at` index has been on the model since the table was written and
is read by nothing else. It is the index this pass needs, and it is fairly
clearly the one it was put there for.

**Why deleting these is safe, and why the default is generous.** The row's
only job is bounded in time: it identifies a delivery that a platform might
*retry*. Meta retries a webhook over hours and days, not weeks; Shopify stops
after 48 hours. A claim older than the retention window can therefore no
longer be matched by anything that would still arrive. Pruning *earlier* than
a platform's retry window is what would re-open the duplicate-order door, so
the default is 30 days -- an order of magnitude past the longest window either
platform uses -- rather than a tight fit against it.

This lives in `domain/` rather than next to the claim in `assistant/runtime.py`
because `domain/services/scheduler.py` is what runs it, and `domain/` never
imports `assistant/`. It touches nothing but a domain model and the session
factory, which is the layer it belongs to anyway.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete

from config.settings import settings
from domain.db import session_scope
from domain.models import WebhookEvent, utcnow

log = logging.getLogger("wanas.retention")


def prune_webhook_events(*, older_than_days: int | None = None) -> int:
    """Delete idempotency claims no platform will still retry against.

    Returns the number of rows removed. Never raises: this is housekeeping,
    and a failure to tidy up must never be the thing that stops a customer
    being answered -- the same contract every optional piece of startup in
    `app.py` is held to.

    `WEBHOOK_EVENT_RETENTION_DAYS=0` disables the pass entirely, which is what
    the test suite uses so a fixed clock cannot delete a claim a test just
    made.
    """
    days = (
        settings.webhook_event_retention_days if older_than_days is None else older_than_days
    )
    if days <= 0:
        return 0

    cutoff = utcnow() - timedelta(days=days)
    try:
        with session_scope() as session:
            deleted = (
                session.execute(
                    delete(WebhookEvent).where(WebhookEvent.received_at < cutoff)
                ).rowcount
                or 0
            )
    except Exception:
        log.exception("could not prune old webhook idempotency claims")
        return 0

    if deleted:
        log.info(
            "pruned %d webhook idempotency claim(s) older than %d day(s)", deleted, days
        )
    return deleted
