"""The other direction from `product_import.py`: a wanas.db product whose
Shopify product is gone.

`product_import` is additive by contract -- it mirrors a Shopify product into
wanas.db and never deletes on either side. That leaves one gap open. Shopify
is the source of truth for price and stock, and the bot matches the two by
SKU; a local product whose SKUs Shopify no longer knows gets no live numbers
at all, falls back to `variants.price`/`variants.stock_qty` (seeded columns
nothing keeps current), and is offered to customers as a real product -- in
production that meant three phantom "One Size" t-shirts at 0 EGP that had
outlived the Shopify products they were mirrored from.

This module closes that gap, and it deletes, so every judgement here is made
the cautious way:

- **A partial read is never "everything is gone."** `all_variant_skus()`
  raises on a failed page rather than returning what it managed, an empty
  live set aborts outright, and `MAX_VANISHED_FRACTION` refuses a run that
  claims most of the catalog disappeared. A shop really does not lose 80% of
  its products between two boots; a wrong token pointed at an empty store
  looks exactly like that.
- **One surviving SKU keeps the whole product.** Partial variant drift is
  `shopify_set_skus.py`'s problem, not this one.
- **Anything ever ordered is archived, never deleted.** `order_items.
  variant_id` is a foreign key and an order is the record that money changed
  hands; it outranks tidying the catalog. Archiving takes it out of the bot's
  search and `get_variants` while the order lines still read -- locally only,
  since there is no Shopify product left to set `status: ARCHIVED` on.
- **It is a script, not a boot step.** `product_import` runs on boot because
  the worst it can do is add a row. This one is `scripts/
  shopify_reconcile_products.py`, dry-run by default like every other
  `scripts/shopify_*.py`, because the worst it can do is delete the catalog.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Product
from integrations.shopify import admin_products as admin

log = logging.getLogger("wanas.shopify.product_reconcile")

#: Refuse a run that says more than this share of the catalog vanished. Not a
#: business rule -- a smoke alarm for a read that went wrong (a token for the
#: wrong store, a query that silently filtered). Pass `force=True` to mean it.
MAX_VANISHED_FRACTION = 0.5


class ReconcileRefused(RuntimeError):
    """The live read did not look trustworthy enough to delete anything from."""


def reconcile_vanished_products(
    session: Session, *, apply: bool = False, force: bool = False
) -> dict:
    """Every wanas.db product whose SKUs Shopify no longer knows.

    Dry run by default: reports what it would do and writes nothing. With
    `apply=True` a product nobody ordered is deleted (along with the cart
    lines and stock-waitlist entries that only pointed at it) and a product
    that sold is archived instead.

    Returns `{"checked", "live_skus", "deleted": [...], "archived": [...],
    "skipped": [...]}`. Raises `ReconcileRefused` when the live read looks
    untrustworthy -- see this module's docstring.
    """
    live = admin.all_variant_skus()
    if not live:
        raise ReconcileRefused(
            "Shopify returned no SKUs at all; that is an outage or the wrong "
            "store, not an empty catalog"
        )

    products = list(session.scalars(select(Product)).all())
    vanished = [p for p in products if not {v.variant_id for v in p.variants} & live]

    if products and len(vanished) / len(products) > MAX_VANISHED_FRACTION and not force:
        raise ReconcileRefused(
            f"{len(vanished)} of {len(products)} products look gone from Shopify "
            f"({len(live)} live SKUs read). That is more than "
            f"{MAX_VANISHED_FRACTION:.0%} of the catalog -- check the store and "
            f"the token before running this with force=True"
        )

    deleted: list[dict] = []
    archived: list[dict] = []
    skipped: list[dict] = []

    for product in vanished:
        skus = sorted(v.variant_id for v in product.variants)
        if not skus:
            # No variants at all: nothing to match on, so "gone from Shopify"
            # is not something this read can claim. Report it and move on.
            skipped.append({"product_id": product.product_id, "why": "no variants to match on"})
            continue

        sold = bool(admin.sold_variant_ids(session, skus))
        row = {"product_id": product.product_id, "name": product.name, "skus": skus}

        if not apply:
            (archived if sold else deleted).append(row)
            continue

        if sold:
            if product.archived:
                skipped.append({**row, "why": "sold and already archived"})
                continue
            product.archived = True
            session.flush()
            log.info("archived %s: sold before, but gone from Shopify", product.product_id)
            archived.append(row)
        else:
            admin.delete_product(session, product.product_id)
            log.info("deleted %s: gone from Shopify, never ordered", product.product_id)
            deleted.append(row)

    return {
        "checked": len(products),
        "live_skus": len(live),
        "deleted": deleted,
        "archived": archived,
        "skipped": skipped,
    }
