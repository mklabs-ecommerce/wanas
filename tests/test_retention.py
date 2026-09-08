"""`webhook_events` must stop growing, without ever letting a live retry back in.

The claim row is what stops a platform retry being processed twice, so the two
things worth proving are opposite: an *old* claim is removed (the table is
bounded), and a *recent* one is not (a retry that could still arrive is still
refused). See `domain/services/retention.py`.
"""

from __future__ import annotations

from datetime import timedelta

from domain.models import WebhookEvent, utcnow
from domain.services import retention


def _claim(db, message_id: str, *, age_days: float) -> None:
    db.add(
        WebhookEvent(
            platform_message_id=message_id,
            received_at=utcnow() - timedelta(days=age_days),
        )
    )
    db.flush()


def _ids(db) -> set[str]:
    return {row.platform_message_id for row in db.query(WebhookEvent).all()}


def test_prune_removes_claims_past_the_retention_window(db):
    _claim(db, "wamid.ancient", age_days=90)
    _claim(db, "wamid.old", age_days=31)
    db.commit()

    removed = retention.prune_webhook_events(older_than_days=30)

    assert removed == 2
    db.expire_all()
    assert _ids(db) == set()


def test_prune_keeps_a_claim_a_platform_could_still_retry(db):
    # Meta retries over hours and days; Shopify gives up at 48h. Anything
    # inside the window has to survive, or the retry it was written to refuse
    # becomes a second order.
    _claim(db, "wamid.yesterday", age_days=1)
    _claim(db, "shopify:fresh", age_days=0)
    _claim(db, "wamid.expired", age_days=45)
    db.commit()

    removed = retention.prune_webhook_events(older_than_days=30)

    assert removed == 1
    db.expire_all()
    assert _ids(db) == {"wamid.yesterday", "shopify:fresh"}


def test_a_kept_claim_still_suppresses_a_duplicate_delivery(db):
    """The point of keeping it: `runtime` must still refuse the retry."""
    from assistant import runtime

    _claim(db, "wamid.recent", age_days=2)
    db.commit()

    retention.prune_webhook_events(older_than_days=30)

    reply = runtime.handle_message(
        "whatsapp", "201000000001", "hello", platform_message_id="wamid.recent", db=db
    )
    assert reply.duplicate is True


def test_retention_disabled_deletes_nothing(db):
    _claim(db, "wamid.ancient", age_days=400)
    db.commit()

    assert retention.prune_webhook_events(older_than_days=0) == 0
    db.expire_all()
    assert _ids(db) == {"wamid.ancient"}


def test_prune_never_raises_when_the_database_is_unreachable(monkeypatch):
    """Housekeeping must not be what stops messages being answered."""

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(retention, "session_scope", boom)
    assert retention.prune_webhook_events(older_than_days=30) == 0


def test_scheduler_tick_runs_the_retention_pass(monkeypatch):
    """It is wired into the one clock this app has, not left to be called."""
    from domain.services.scheduler import Scheduler

    calls = []
    monkeypatch.setattr(retention, "prune_webhook_events", lambda: calls.append(True))
    for name in ("check_back_in_stock", "check_abandoned_carts"):
        monkeypatch.setattr(
            "domain.services.reengagement." + name, lambda *a, **k: 0
        )
    monkeypatch.setattr(
        "integrations.instagram.token.scheduled_refresh", lambda *a, **k: None
    )

    Scheduler(interval_seconds=0)._tick()

    assert calls == [True]
