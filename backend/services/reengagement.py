"""Two time-based follow-ups, neither triggered by an event.

Nothing tells this app "that variant is back in stock" or "six hours passed"
-- Shopify pushes order-status webhooks, not inventory ones, and an idle cart
is defined by the *absence* of a message. Both checks are therefore polls,
run periodically by `backend/services/scheduler.py`, and both are written to
be safe to run twice: a re-run before the last one's commit lands just finds
the same open row again and no second message goes out, because the row that
marks "handled" is written in the same pass that sends.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select

from backend.config import settings
from backend.db import session_scope
from backend.models import AbandonedCartNudge, CartItem, StockWaitlistEntry, Variant, utcnow
from backend.services import notifications, shopify_catalog, waitlist

log = logging.getLogger("wanas.reengagement")


def check_back_in_stock() -> int:
    """Notify everyone waiting on a variant that is now available.

    Reads Shopify once for the whole open waitlist rather than once per
    entry -- several customers can be waiting on the same variant, and this
    is a background pass with no customer waiting on the reply.
    """
    with session_scope() as session:
        entries = waitlist.open_entries(session)
    if not entries:
        return 0

    variant_ids = {e.variant_id for e in entries}
    try:
        live = shopify_catalog.fetch_skus(variant_ids)
    except (shopify_catalog.ShopifyUnavailable, shopify_catalog.ShopifyConfigError) as exc:
        log.warning("back-in-stock check skipped: %s", exc)
        return 0

    notified = 0
    for entry in entries:
        live_variant = live.get(entry.variant_id)
        if live_variant is None or not live_variant.product_active or live_variant.stock_qty <= 0:
            continue

        with session_scope() as session:
            # Re-read inside the notifying transaction: another process (or an
            # earlier entry for the same variant, already handled this pass)
            # may have closed it out first.
            fresh = session.get(StockWaitlistEntry, entry.id)
            if fresh is None or fresh.notified_at is not None:
                continue

            variant = session.get(Variant, fresh.variant_id)
            product_name = variant.product.name if variant else fresh.variant_id
            notifications.send_proactive(
                session,
                fresh.channel,
                fresh.external_id,
                notifications.BACK_IN_STOCK_TEXT.format(product_name=product_name),
                template=settings.whatsapp_template_back_in_stock or None,
                alert_reason="proactive_outreach_failed",
                alert_summary=f"Back-in-stock notice for {product_name} to {fresh.external_id} needs a hand",
                alert_payload={"variant_id": fresh.variant_id},
            )
            fresh.notified_at = utcnow()
            notified += 1

    return notified


def check_abandoned_carts() -> int:
    """Nudge every WhatsApp cart that has sat untouched past the threshold.

    `MAX(added_at)` per identity stands in for "last touched": a quantity
    bump on an existing line does not move it (`carts.add` never rewrites
    `added_at`), so a customer quietly upping a quantity mid-window is a rare
    case this slightly under-counts as idle, never one it wrongly interrupts.
    """
    now = utcnow()
    min_idle = timedelta(hours=settings.abandoned_cart_hours)
    max_idle = timedelta(hours=settings.abandoned_cart_max_age_hours)

    # The comparison happens in Python, not the WHERE clause -- the same
    # choice `chatbot/session.py`'s expiry check makes, because nothing else
    # in this codebase relies on a database comparing a tz-aware Python value
    # against a stored `DateTime(timezone=True)` column, and this is not the
    # place to find out whether SQLite's string-typed storage agrees with
    # PostgreSQL's on ordering across that boundary. The candidate set here
    # is every open WhatsApp cart, never large enough for this to cost
    # anything.
    with session_scope() as session:
        rows = session.execute(
            select(
                CartItem.channel,
                CartItem.external_id,
                func.max(CartItem.added_at).label("last_activity"),
            )
            .where(CartItem.channel == "whatsapp")
            .group_by(CartItem.channel, CartItem.external_id)
        ).all()

    due = [
        (channel, external_id, last_activity)
        for channel, external_id, last_activity in rows
        if min_idle <= now - last_activity < max_idle
    ]

    nudged = 0
    for channel, external_id, last_activity in due:
        with session_scope() as session:
            nudge = session.get(AbandonedCartNudge, (channel, external_id))
            if nudge is not None and nudge.sent_at >= last_activity:
                # Already nudged for this exact idle spell; a new line after
                # the nudge would have moved `last_activity` past it.
                continue

            notifications.send_proactive(
                session,
                channel,
                external_id,
                notifications.ABANDONED_CART_TEXT,
                template=settings.whatsapp_template_abandoned_cart or None,
                alert_reason="proactive_outreach_failed",
                alert_summary=f"Abandoned-cart nudge to {external_id} needs a person",
            )
            if nudge is None:
                session.add(AbandonedCartNudge(channel=channel, external_id=external_id, sent_at=now))
            else:
                nudge.sent_at = now
            nudged += 1

    return nudged
