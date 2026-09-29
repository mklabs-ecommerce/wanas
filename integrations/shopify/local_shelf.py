"""The shop with no Shopify behind it: the local database is the shelf and the till.

Rehla runs without a Shopify store. Rather than teach every caller a second
code path, the handful of Shopify functions the bot's sales path touches ask
`active()` first and answer from here -- the same seam `tests/fake_shopify.py`
patches, so the catalog overlay, the cart and `place_order` behave exactly as
they do against a real store:

* `fetch_all` / `fetch_skus` read `variants` (price, compare-at, stock_qty).
* `reserve` / `release` are no-ops: `inventory.decrement` / `record_sold`
  already write the local row, which is the only stock there is.
* `create_order` hands back an order with no Shopify id; the local order row
  is the record, and its own `order_id` is the reference the customer gets.
* Order *edits* on Shopify are refused as unavailable -- staff handle those.

Off whenever Shopify credentials are set, or with `LOCAL_STORE=0`.
"""

from __future__ import annotations

from decimal import Decimal

from config.settings import settings


def active() -> bool:
    return settings.local_store_active


def _live(row):
    from integrations.shopify.catalog import LiveVariant

    variant_id, price, original, stock_qty, archived = row
    price = Decimal(str(price))
    original = Decimal(str(original if original is not None else price))
    return LiveVariant(
        variant_id=variant_id,
        shopify_id=f"local:{variant_id}",
        inventory_item_id=f"local:{variant_id}",
        price=price,
        original_price=original if original > price else price,
        stock_qty=int(stock_qty or 0),
        tracked=True,
        product_active=not bool(archived),
        image_url=None,
    )


_SHELF_SQL = (
    "SELECT v.variant_id, v.price, v.original_price, v.stock_qty, p.archived "
    "FROM variants v JOIN products p ON p.product_id = v.product_id"
)


def fetch(variant_ids=None) -> dict:
    """Local variants as `LiveVariant`s, keyed by variant_id. All of them
    when `variant_ids` is None.

    Read on a raw DBAPI connection, never a Session: this runs *inside* a turn
    whose own session may already hold SQLite's write lock (every transaction
    there is BEGIN IMMEDIATE, `domain/db.py`), and a second session would
    wait on it forever. A plain SELECT under WAL reads alongside the writer.
    """
    from domain.db import engine

    wanted = None if variant_ids is None else {str(v) for v in variant_ids if v}
    if wanted is not None and not wanted:
        return {}
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute(_SHELF_SQL)
        rows = cursor.fetchall()
        cursor.close()
    finally:
        raw.close()
    return {row[0]: _live(row) for row in rows if wanted is None or row[0] in wanted}


def create_order(**_kwargs) -> dict:
    return {"id": None, "name": None}
