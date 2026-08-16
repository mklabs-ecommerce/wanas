"""The live read: what the customer is told when Shopify and wanas.db differ.

Nothing here touches the network. `shopify_catalog.prime` injects the snapshot
the client would have returned, which is the whole point of keeping the fetch
and the overlay in separate functions.

The cases worth pinning down are the ones where the two sources disagree, and
the ones where Shopify cannot be reached at all -- a bot that silently reverts
to a stale price is worse than one that never moved to Shopify, because nobody
would know.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.models import Variant
from backend.services import catalog, inventory, shopify_catalog
from backend.services.shopify_catalog import LiveVariant


def live(variant_id, price, stock, *, compare=None, active=True):
    price = Decimal(str(price))
    return LiveVariant(
        variant_id=variant_id,
        shopify_id=f"gid://shopify/ProductVariant/{abs(hash(variant_id)) % 10**9}",
        inventory_item_id=f"gid://shopify/InventoryItem/{variant_id}",
        price=price,
        original_price=Decimal(str(compare)) if compare is not None else price,
        stock_qty=stock,
        tracked=True,
        product_active=active,
    )


def snapshot_of(db, product_id, **overrides):
    """Start from what wanas.db says, then change only what a test cares
    about. Keeps each test about one difference."""
    out = {}
    for variant in db.query(Variant).filter(Variant.product_id == product_id):
        out[variant.variant_id] = live(
            variant.variant_id, variant.price, variant.stock_qty
        )
    out.update(overrides)
    return out


@pytest.fixture
def turn():
    with shopify_catalog.turn_scope():
        yield


# --------------------------------------------------------------------------
# price
# --------------------------------------------------------------------------


def test_the_quoted_price_is_shopifys_not_the_databases(seeded, turn):
    target = seeded.query(Variant).filter_by(product_id="wanas-hoodie").first()
    shopify_catalog.prime(
        snapshot_of(seeded, "wanas-hoodie", **{target.variant_id: live(target.variant_id, 999, 5)})
    )

    payload = catalog.get_variants(seeded, "wanas-hoodie")
    quoted = next(v for v in payload["variants"] if v["variant_id"] == target.variant_id)

    assert quoted["price"] == 999
    # And the row itself was not rewritten on the way past.
    assert seeded.get(Variant, target.variant_id).price != 999


def test_a_products_price_range_follows_shopify(seeded, turn):
    variants = seeded.query(Variant).filter_by(product_id="wanas-hoodie").all()
    snapshot = {v.variant_id: live(v.variant_id, 650, 5) for v in variants}
    snapshot[variants[0].variant_id] = live(variants[0].variant_id, 700, 5)
    shopify_catalog.prime(snapshot)

    listed = catalog.get_products(seeded)["products"]
    product = next(p for p in listed if p["product_id"] == "wanas-hoodie")
    assert product["price_from"] == 650
    assert product["price_to"] == 700


def test_a_discount_is_read_from_compare_at_price(seeded, turn):
    target = seeded.query(Variant).filter_by(product_id="wanas-hoodie").first()
    shopify_catalog.prime(
        snapshot_of(
            seeded,
            "wanas-hoodie",
            **{target.variant_id: live(target.variant_id, 600, 5, compare=800)},
        )
    )

    quoted = next(
        v
        for v in catalog.get_variants(seeded, "wanas-hoodie")["variants"]
        if v["variant_id"] == target.variant_id
    )
    assert quoted["on_sale"] is True
    assert quoted["original_price"] == 800


def test_no_compare_at_price_means_no_sale(seeded, turn):
    target = seeded.query(Variant).filter_by(product_id="wanas-hoodie").first()
    shopify_catalog.prime(
        snapshot_of(seeded, "wanas-hoodie", **{target.variant_id: live(target.variant_id, 600, 5)})
    )

    quoted = next(
        v
        for v in catalog.get_variants(seeded, "wanas-hoodie")["variants"]
        if v["variant_id"] == target.variant_id
    )
    assert quoted["on_sale"] is False
    assert quoted["original_price"] == 600


# --------------------------------------------------------------------------
# stock
# --------------------------------------------------------------------------


def test_sold_out_on_shopify_is_sold_out_here_even_if_the_row_says_otherwise(seeded, turn):
    stocked = seeded.query(Variant).filter(Variant.stock_qty > 0).first()
    shopify_catalog.prime(
        snapshot_of(
            seeded,
            stocked.product_id,
            **{stocked.variant_id: live(stocked.variant_id, stocked.price, 0)},
        )
    )

    payload = catalog.get_variants(seeded, stocked.product_id)
    quoted = next(v for v in payload["variants"] if v["variant_id"] == stocked.variant_id)

    assert quoted["status"] == "sold_out"
    assert stocked.variant_id not in payload["in_stock"]
    assert inventory.available(seeded, stocked.variant_id) == 0


def test_restocked_on_shopify_becomes_available_here(seeded, turn):
    empty = seeded.query(Variant).filter(Variant.stock_qty == 0).first()
    shopify_catalog.prime(
        snapshot_of(
            seeded, empty.product_id, **{empty.variant_id: live(empty.variant_id, empty.price, 7)}
        )
    )

    payload = catalog.get_variants(seeded, empty.product_id)
    assert empty.variant_id in payload["in_stock"]
    assert inventory.available(seeded, empty.variant_id) == 7


def test_a_product_not_active_on_shopify_reads_as_sold_out(seeded, turn):
    stocked = seeded.query(Variant).filter(Variant.stock_qty > 0).first()
    shopify_catalog.prime(
        snapshot_of(
            seeded,
            stocked.product_id,
            **{
                stocked.variant_id: live(
                    stocked.variant_id, stocked.price, 12, active=False
                )
            },
        )
    )

    payload = catalog.get_variants(seeded, stocked.product_id)
    quoted = next(v for v in payload["variants"] if v["variant_id"] == stocked.variant_id)
    assert quoted["status"] == "sold_out"


def test_alternatives_are_only_offered_for_stock_shopify_confirms(seeded, turn):
    variants = seeded.query(Variant).filter_by(product_id="wanas-hoodie").all()
    wanted, *siblings = variants

    # Everything sold out on Shopify, whatever wanas.db believes.
    shopify_catalog.prime({v.variant_id: live(v.variant_id, v.price, 0) for v in variants})
    assert catalog.alternatives_for(seeded, wanted) == []

    # One sibling back on the shelf, and it is the only thing offered.
    snapshot = {v.variant_id: live(v.variant_id, v.price, 0) for v in variants}
    snapshot[siblings[0].variant_id] = live(siblings[0].variant_id, siblings[0].price, 3)
    shopify_catalog.prime(snapshot)

    offered = catalog.alternatives_for(seeded, wanted)
    assert [a["variant_id"] for a in offered] == [siblings[0].variant_id]


# --------------------------------------------------------------------------
# degrading
# --------------------------------------------------------------------------


def test_when_shopify_cannot_be_reached_the_local_numbers_are_served(seeded, turn):
    shopify_catalog.prime(None)

    row = seeded.query(Variant).filter_by(product_id="wanas-hoodie").first()
    quoted = next(
        v
        for v in catalog.get_variants(seeded, "wanas-hoodie")["variants"]
        if v["variant_id"] == row.variant_id
    )
    assert quoted["price"] == row.price
    assert quoted["stock_qty"] == row.stock_qty


def test_a_variant_missing_from_shopify_falls_back_rather_than_vanishing(seeded, turn):
    """No SKU written yet, or the product was deleted from the store. Serving
    the local row is wrong-ish; dropping the variant from the reply entirely
    would be worse, because the bot would tell the customer the size does not
    exist."""
    row = seeded.query(Variant).filter_by(product_id="wanas-hoodie").first()
    snapshot = snapshot_of(seeded, "wanas-hoodie")
    del snapshot[row.variant_id]
    shopify_catalog.prime(snapshot)

    payload = catalog.get_variants(seeded, "wanas-hoodie")
    quoted = next(v for v in payload["variants"] if v["variant_id"] == row.variant_id)
    assert quoted["price"] == row.price
    assert quoted["stock_qty"] == row.stock_qty


def test_an_untracked_variant_is_not_mistaken_for_sold_out(seeded, turn):
    row = seeded.query(Variant).filter_by(product_id="wanas-hoodie").first()
    untracked = LiveVariant(
        variant_id=row.variant_id,
        shopify_id="gid://shopify/ProductVariant/1",
        inventory_item_id="gid://shopify/InventoryItem/1",
        price=Decimal("650"),
        original_price=Decimal("650"),
        stock_qty=999,
        tracked=False,
        product_active=True,
    )
    shopify_catalog.prime(snapshot_of(seeded, "wanas-hoodie", **{row.variant_id: untracked}))

    payload = catalog.get_variants(seeded, "wanas-hoodie")
    assert row.variant_id in payload["in_stock"]


# --------------------------------------------------------------------------
# the turn boundary
# --------------------------------------------------------------------------


def test_the_snapshot_is_taken_once_per_turn_not_once_per_tool(seeded, monkeypatch):
    calls = []

    def counted():
        calls.append(1)
        return {}

    monkeypatch.setattr(shopify_catalog, "try_fetch_all", counted)

    with shopify_catalog.turn_scope():
        catalog.get_products(seeded, query="hoodie")
        catalog.get_variants(seeded, "wanas-hoodie")
        catalog.get_products(seeded, category="T-Shirts")

    assert len(calls) == 1


def test_a_failure_is_not_retried_three_times_inside_one_reply(seeded, monkeypatch):
    calls = []

    def failing():
        calls.append(1)
        return None

    monkeypatch.setattr(shopify_catalog, "try_fetch_all", failing)

    with shopify_catalog.turn_scope():
        catalog.get_products(seeded, query="hoodie")
        catalog.get_variants(seeded, "wanas-hoodie")

    assert len(calls) == 1


def test_the_next_turn_reads_the_shelf_again(seeded, monkeypatch):
    calls = []
    monkeypatch.setattr(shopify_catalog, "try_fetch_all", lambda: calls.append(1) or {})

    for _ in range(3):
        with shopify_catalog.turn_scope():
            catalog.get_products(seeded, query="hoodie")

    assert len(calls) == 3
