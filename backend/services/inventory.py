"""Inventory service.

Owns `stock_qty` and `low_stock_threshold` per variant -- the single source of
truth every other service checks against. No other module writes `stock_qty`
through the order path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import case, update
from sqlalchemy.orm import Session

from backend.models import Variant
from backend.services import shopify_catalog, shopify_inventory

log = logging.getLogger("wanas.inventory")


@dataclass(frozen=True)
class StockResult:
    ok: bool
    variant_id: str
    available: int
    #: Why it failed. "insufficient" is the only one that means "sold out" --
    #: the others are the shop's problem, not the customer's, and must not be
    #: reported to them as an empty shelf.
    reason: str = "ok"
    #: What was taken off the Shopify shelf, so a caller whose own transaction
    #: later fails can put it back. None when nothing was reserved.
    receipt: dict | None = None


def available(session: Session, variant_id: str) -> int:
    """What Shopify says is on the shelf, falling back to the local row.

    Used for browsing and for the cart. The order path calls `available_now`
    instead, because at that moment "could not check" must not read as zero.
    """
    variant = session.get(Variant, variant_id)
    if variant is None:
        return 0

    live_map = shopify_catalog.live_map()
    if live_map is not None:
        live = live_map.get(variant_id)
        if live is not None:
            return live.stock_qty if live.product_active else 0
    return variant.stock_qty


def available_now(variant_id: str) -> int | None:
    """A single fresh read, bypassing the per-turn snapshot.

    None means Shopify could not be reached -- deliberately distinct from 0.
    A conversation can run for several minutes between "add it to the cart" and
    "yes, confirm", which is long enough for the storefront to sell the last
    one, so the snapshot taken at the start of the turn is not good enough to
    take money on.
    """
    try:
        found = shopify_catalog.fetch_skus([variant_id])
    except (shopify_catalog.ShopifyUnavailable, shopify_catalog.ShopifyConfigError):
        return None
    live = found.get(variant_id)
    if live is None:
        return None
    return live.stock_qty if live.product_active else 0


def decrement(
    session: Session,
    variant_id: str,
    quantity: int,
    *,
    live=None,
    order_ref: str = "pending",
) -> StockResult:
    """Take stock off the shelf -- Shopify's shelf first, then the local row.

    Shopify decides. It is the shop the storefront also sells from, so a check
    against `wanas.db` here would be checking a copy while the original moves.
    The old `WHERE stock_qty >= :n` guard was the right idea against a race
    between two conversations; the equivalent now is `compareQuantity` on the
    Shopify adjustment, which makes Shopify refuse the write if the number
    moved -- covering the storefront too, which the SQL never could.

    `live` lets a caller that already fetched the variant pass it in; without
    it this makes its own round trip.

    The local row is updated afterwards so the dashboard and any Shopify
    outage keep seeing something sane. It is bookkeeping now, not the decision.
    """
    if quantity <= 0:
        return StockResult(False, variant_id, available(session, variant_id), "bad_quantity")

    if live is None:
        try:
            live = shopify_catalog.fetch_skus([variant_id]).get(variant_id)
        except (shopify_catalog.ShopifyUnavailable, shopify_catalog.ShopifyConfigError) as exc:
            log.warning("Could not check stock for %s: %s", variant_id, exc)
            return StockResult(False, variant_id, 0, "store_unavailable")

    if live is None:
        # Reached Shopify, but this variant is not there -- no SKU, or deleted
        # from the store. Refusing is the only honest answer: we cannot take
        # the item off a shelf we cannot find.
        log.warning("%s has no Shopify variant; refusing to sell it", variant_id)
        return StockResult(False, variant_id, 0, "not_on_shopify")

    if not live.product_active:
        return StockResult(False, variant_id, 0, "insufficient")

    if live.tracked and live.stock_qty < quantity:
        return StockResult(False, variant_id, live.stock_qty, "insufficient")

    receipt = {
        "inventory_item_id": live.inventory_item_id,
        "quantity": quantity,
        "expected": live.stock_qty - quantity,
        "variant_id": variant_id,
    }

    if live.tracked:
        try:
            shopify_inventory.reserve(
                [
                    {
                        "inventory_item_id": live.inventory_item_id,
                        "quantity": quantity,
                        "expected": live.stock_qty,
                    }
                ],
                order_ref,
            )
        except shopify_inventory.StockMoved:
            # Someone else got it between our read and our write. Not an
            # outage, and not a plain "sold out" either -- re-read so the
            # customer is told the truth.
            fresh = 0
            try:
                found = shopify_catalog.fetch_skus([variant_id]).get(variant_id)
                fresh = found.stock_qty if found else 0
            except Exception:  # pragma: no cover - already degraded
                pass
            return StockResult(False, variant_id, fresh, "stock_moved")
        except (shopify_catalog.ShopifyUnavailable, shopify_catalog.ShopifyConfigError) as exc:
            log.warning("Could not reserve %s on Shopify: %s", variant_id, exc)
            return StockResult(False, variant_id, live.stock_qty, "store_unavailable")
    else:
        receipt = None

    _local_decrement(session, variant_id, quantity)
    return StockResult(True, variant_id, available(session, variant_id), "ok", receipt)


def record_sold(session: Session, variant_id: str, quantity: int) -> None:
    """Write down a sale Shopify has already taken off its own shelf.

    The order path uses this rather than `decrement`: `orderCreate` decrements
    Shopify as part of creating the order, so calling `decrement` there would
    take every item twice. Bookkeeping only -- it touches nothing but the local
    row, and it cannot refuse.
    """
    if quantity <= 0:
        return
    _local_decrement(session, variant_id, quantity)


def record_returned(session: Session, variant_id: str, quantity: int) -> None:
    """The mirror of `record_sold`: Shopify already put it back.

    A cancellation restocks through Shopify's own order cancel. This keeps the
    local row in step without adding the units a second time.
    """
    if quantity <= 0:
        return
    session.execute(
        update(Variant)
        .where(Variant.variant_id == variant_id)
        .values(stock_qty=Variant.stock_qty + quantity)
    )
    session.expire_all()


def _local_decrement(session: Session, variant_id: str, quantity: int) -> None:
    """Unconditional, and floored at zero.

    It used to refuse to go negative by refusing to write at all, because it
    was the decision. Now that Shopify has already agreed to the sale, a local
    row that happens to disagree must not be allowed to veto it -- but it must
    not go negative either.

    A `CASE WHEN`, not `func.max(a, b)`: SQLite's `max()` is overloaded to
    also act as a 2-argument scalar function, but PostgreSQL's `max()` is
    aggregate-only -- `max(a, b)` is `UndefinedFunction` there, every time,
    unconditionally. That silently passed every test against the SQLite
    default and broke every single order placement in production (Postgres)
    at the last step, after the Shopify order had already been created. See
    AGENTS.md / CLAUDE.md on running the suite against Postgres before
    deploying -- this is exactly the failure that warning is about. `CASE
    WHEN` is plain SQL-92 and compiles identically on both.
    """
    session.execute(
        update(Variant)
        .where(Variant.variant_id == variant_id)
        .values(stock_qty=case((Variant.stock_qty - quantity > 0, Variant.stock_qty - quantity), else_=0))
    )
    session.expire_all()


def release(session: Session, variant_id: str, quantity: int, *, order_ref: str = "release") -> None:
    """Give stock back -- a cancellation or a quantity decrease.

    Best effort against Shopify: a cancellation the customer has been promised
    must not fail because the store is briefly unreachable. A failure here is
    logged loudly enough to be fixed by hand.
    """
    if quantity <= 0:
        return

    try:
        live = shopify_catalog.fetch_skus([variant_id]).get(variant_id)
    except (shopify_catalog.ShopifyUnavailable, shopify_catalog.ShopifyConfigError) as exc:
        live = None
        log.error("Could not reach Shopify to restock %s (+%s): %s", variant_id, quantity, exc)

    if live is not None and live.tracked:
        shopify_inventory.try_release(
            [
                {
                    "inventory_item_id": live.inventory_item_id,
                    "quantity": quantity,
                    "expected": live.stock_qty,
                }
            ],
            order_ref,
        )

    session.execute(
        update(Variant)
        .where(Variant.variant_id == variant_id)
        .values(stock_qty=Variant.stock_qty + quantity)
    )
    session.expire_all()


def return_receipts(receipts, order_ref: str = "rollback") -> None:
    """Put back everything a failed transaction had already reserved.

    Shopify has no savepoint. When the local write rolls back, this is what
    stands in for it.
    """
    real = [r for r in receipts if r]
    if not real:
        return
    shopify_inventory.try_release(
        [
            {
                "inventory_item_id": r["inventory_item_id"],
                "quantity": r["quantity"],
                "expected": r["expected"],
            }
            for r in real
        ],
        order_ref,
    )


def breached_threshold(session: Session, variant_id: str) -> bool:
    """Checked after every decrement. Crossing the threshold is what publishes
    `low_stock_breach`; sold out fires at zero regardless of the threshold."""
    variant = session.get(Variant, variant_id)
    if variant is None:
        return False
    return variant.stock_qty <= variant.low_stock_threshold


