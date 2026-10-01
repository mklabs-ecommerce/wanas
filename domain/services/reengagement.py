"""The back-in-stock follow-up, which no event triggers.

(The idle-cart nudge that used to live here is now nudge #2 of the general
"customer went silent" follow-up, `assistant/silence_nudges.py`.)

Nothing tells this app "that variant is back in stock" -- Shopify pushes
order-status webhooks, not inventory ones. The check is therefore a poll,
run periodically by `domain/services/scheduler.py`, and written to be safe
to run twice: a re-run before the last one's commit lands just finds
the same open row again and no second message goes out, because the row that
marks "handled" is written in the same pass that sends.
"""

from __future__ import annotations

import logging

from config.settings import settings
from domain.db import session_scope
from domain.models import (
    StockWaitlistEntry,
    Variant,
    utcnow,
)
from domain.services import (
    notifications,
    waitlist,
)
from integrations.shopify import catalog as shopify_catalog

log = logging.getLogger("rehla.reengagement")


def check_back_in_stock() -> int:
    """Notify everyone waiting on a variant that is now available.

    Reads Shopify once for the whole open waitlist rather than once per
    entry -- several customers can be waiting on the same variant, and this
    is a background pass with no customer waiting on the reply.

    "Available now" is not on its own a reason to say anything. The message
    this sends claims an *event* -- that the item came back -- so it goes out
    only where both ends of that event are on record: `observed_stock` at or
    below zero when the customer was turned away, and above zero now. An
    entry created against a level nobody verified is baselined and left
    alone. Before that, a rehla.db row whose `stock_qty` had gone stale at
    zero was enough to produce a restock announcement for an item that had
    been in stock the whole time.
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

            if fresh.observed_stock is None:
                # Written before `observed_stock` existed, so there is no
                # baseline to compare against and no way to tell a real
                # restock from an entry that was created against a stale zero
                # in the first place. Baseline it from this reading and say
                # nothing: if it genuinely goes to zero and back later, that
                # transition is one this can prove.
                fresh.observed_stock = live_variant.stock_qty
                continue

            if fresh.observed_stock > 0:
                # It was on the shelf when this customer was turned away, so
                # it being on the shelf now is not news -- there was no
                # restock to announce. Nothing here can put it right for that
                # customer, but claiming an event that never happened is
                # worse than staying quiet.
                log.info(
                    "waitlist entry %s was created against a non-zero level (%s); "
                    "no restock to announce",
                    fresh.id,
                    fresh.observed_stock,
                )
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

