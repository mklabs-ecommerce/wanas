"""`integrations/shopify/product_reconcile.py`: a wanas.db product whose
Shopify product is gone.

`product_import` only ever adds, so nothing used to remove the local half of
a product deleted in Shopify Admin. The bot kept offering it -- with no live
price or stock, so at whatever the seeded columns happened to say. Every test
here is about the deleting being *narrow*: this is the one reconcile in the
codebase that can destroy the catalog if its read is wrong.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from domain.models import CartItem, Client, Order, OrderItem, Product, StockWaitlistEntry
from domain.services.catalog import get_products
from integrations.shopify import admin_products as sap
from integrations.shopify.product_reconcile import (
    ReconcileRefused,
    reconcile_vanished_products,
)


def _local_only_product(session, shopify, title="Ghost Tee"):
    """A product in wanas.db that Shopify has never heard of -- what a
    deleted-in-Admin product leaves behind."""
    result = sap.create_product(
        session,
        title=title,
        description="",
        category="T-Shirts",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=[{"size": "S", "color": "Olive", "price": 300, "stock_qty": 1}],
    )
    # ...and now it is deleted from Shopify, the local rows left behind.
    shopify.shopify_delete_product(result["shopify_id"])
    session.flush()
    return result


def _sell_one(session, variant_id, order_id="WNS-REC-1"):
    client = Client(full_name="Sara", phone="201000000001", address="somewhere")
    session.add(client)
    session.flush()
    session.add(Order(
        order_id=order_id, client_id=client.client_id, source_channel="whatsapp",
        shipping_address="somewhere", contact_phone="201000000001", governorate="Cairo",
        subtotal=Decimal("300"), shipping_fee=Decimal("60"), total=Decimal("360"),
        status="Confirmed",
    ))
    session.add(OrderItem(
        order_id=order_id, variant_id=variant_id, product_name="Ghost Tee",
        size="S", color="Olive", quantity=1,
        unit_price=Decimal("300"), unit_original_price=Decimal("300"),
    ))
    session.flush()


def test_a_product_shopify_still_knows_is_left_alone(seeded, shopify):
    report = reconcile_vanished_products(seeded, apply=True)

    assert report["deleted"] == []
    assert report["archived"] == []
    assert seeded.get(Product, "wanas-hoodie") is not None


def test_a_product_gone_from_shopify_and_never_ordered_is_deleted(seeded, shopify):
    result = _local_only_product(seeded, shopify)

    report = reconcile_vanished_products(seeded, apply=True)
    seeded.flush()

    assert [r["product_id"] for r in report["deleted"]] == [result["product_id"]]
    assert seeded.get(Product, result["product_id"]) is None


def test_it_stops_the_bot_offering_the_phantom(seeded, shopify):
    """The whole point: with no Shopify match the product had no live price or
    stock and was answered out of the seeded columns instead."""
    result = _local_only_product(seeded, shopify)
    assert result["product_id"] in {p["product_id"] for p in get_products(seeded)["products"]}

    reconcile_vanished_products(seeded, apply=True)
    seeded.flush()

    assert result["product_id"] not in {p["product_id"] for p in get_products(seeded)["products"]}


def test_a_product_that_sold_is_archived_not_deleted(seeded, shopify):
    """An order is the record that money changed hands. It outranks tidying
    the catalog -- and `order_items.variant_id` is a foreign key besides."""
    result = _local_only_product(seeded, shopify)
    _sell_one(seeded, "ghost-tee-s-olive")

    report = reconcile_vanished_products(seeded, apply=True)
    seeded.flush()

    assert report["deleted"] == []
    assert [r["product_id"] for r in report["archived"]] == [result["product_id"]]
    assert seeded.get(Product, result["product_id"]).archived is True
    assert seeded.scalars(
        select(OrderItem).where(OrderItem.variant_id == "ghost-tee-s-olive")
    ).all() != []


def test_an_already_archived_one_is_reported_not_re_archived(seeded, shopify):
    result = _local_only_product(seeded, shopify)
    _sell_one(seeded, "ghost-tee-s-olive")
    reconcile_vanished_products(seeded, apply=True)
    seeded.flush()

    report = reconcile_vanished_products(seeded, apply=True)

    assert report["archived"] == []
    assert [r["product_id"] for r in report["skipped"]] == [result["product_id"]]


def test_one_surviving_sku_keeps_the_whole_product(seeded, shopify):
    """Partial variant drift is `shopify_set_skus.py`'s problem. Deleting a
    product because one of its sizes went is the wrong-sized answer."""
    result = sap.create_product(
        seeded, title="Half Gone Tee", description="", category="T-Shirts",
        department="unisex", style=None, collection=None, size_chart=None,
        variants=[
            {"size": "S", "color": "Olive", "price": 300, "stock_qty": 1},
            {"size": "M", "color": "Olive", "price": 300, "stock_qty": 1},
        ],
    )
    shopify.shopify_delete_variants(result["shopify_id"], ["half-gone-tee-m-olive"])

    report = reconcile_vanished_products(seeded, apply=True)
    seeded.flush()

    assert report["deleted"] == []
    assert seeded.get(Product, result["product_id"]) is not None


def test_the_carts_and_waitlists_pointing_at_it_let_go(seeded, shopify):
    result = _local_only_product(seeded, shopify)
    seeded.add(CartItem(channel="whatsapp", external_id="201000000000",
                        variant_id="ghost-tee-s-olive", quantity=1))
    seeded.add(StockWaitlistEntry(channel="whatsapp", external_id="201000000000",
                                  variant_id="ghost-tee-s-olive", observed_stock=0))
    seeded.flush()

    reconcile_vanished_products(seeded, apply=True)
    seeded.flush()

    assert seeded.scalars(
        select(CartItem).where(CartItem.variant_id == "ghost-tee-s-olive")
    ).all() == []
    assert seeded.scalars(
        select(StockWaitlistEntry).where(StockWaitlistEntry.variant_id == "ghost-tee-s-olive")
    ).all() == []
    assert result["product_id"]


# --------------------------------------------------------------------------
# the guards -- this is the one reconcile that can destroy the catalog
# --------------------------------------------------------------------------


def test_a_dry_run_reports_and_writes_nothing(seeded, shopify):
    result = _local_only_product(seeded, shopify)

    report = reconcile_vanished_products(seeded)
    seeded.flush()

    assert [r["product_id"] for r in report["deleted"]] == [result["product_id"]]
    assert seeded.get(Product, result["product_id"]) is not None


def test_an_empty_live_read_is_refused_outright(seeded, shopify, monkeypatch):
    """No SKUs at all is an outage or the wrong store. Reading it as an empty
    catalog would delete every product in wanas.db."""
    monkeypatch.setattr(sap, "all_variant_skus", set)

    with pytest.raises(ReconcileRefused, match="outage or the wrong store"):
        reconcile_vanished_products(seeded, apply=True)

    assert seeded.get(Product, "wanas-hoodie") is not None


def test_most_of_the_catalog_vanishing_is_refused(seeded, shopify, monkeypatch):
    """A shop does not lose most of its products between two runs. A query
    that silently filtered looks exactly like one that did."""
    survivor = {"wanas-hoodie-s-olive"}
    monkeypatch.setattr(sap, "all_variant_skus", lambda: survivor)

    with pytest.raises(ReconcileRefused, match="more than 50%"):
        reconcile_vanished_products(seeded, apply=True)

    assert seeded.get(Product, "wanas-crewneck") is not None


def test_force_is_what_says_you_meant_it(seeded, shopify, monkeypatch):
    survivor = {"wanas-hoodie-s-olive"}
    monkeypatch.setattr(sap, "all_variant_skus", lambda: survivor)

    report = reconcile_vanished_products(seeded, apply=True, force=True)
    seeded.flush()

    assert len(report["deleted"]) > 1
    assert seeded.get(Product, "wanas-hoodie") is not None


def test_force_does_not_lift_the_empty_read_refusal(seeded, shopify, monkeypatch):
    """The fraction guard is a judgement call. An empty read is not one."""
    monkeypatch.setattr(sap, "all_variant_skus", set)

    with pytest.raises(ReconcileRefused):
        reconcile_vanished_products(seeded, apply=True, force=True)


def test_a_failed_page_is_raised_not_shortened(seeded, shopify, monkeypatch):
    """`all_variant_skus` raising is what keeps a half-read page from reading
    as "those products are gone"."""
    def boom():
        raise RuntimeError("shopify timed out")

    monkeypatch.setattr(sap, "all_variant_skus", boom)

    with pytest.raises(RuntimeError, match="timed out"):
        reconcile_vanished_products(seeded, apply=True)

    assert seeded.get(Product, "wanas-hoodie") is not None


def test_a_product_with_no_variants_is_reported_not_deleted(seeded, shopify):
    """Nothing to match on means "gone from Shopify" is not a claim this read
    can make."""
    seeded.add(Product(
        product_id="empty-shell", name="Empty Shell", category="T-Shirts",
        department="unisex", style=[], sizes=[], colors=[], lengths=[],
        price=Decimal("0"), original_price=Decimal("0"),
        images=[], color_images={}, description="", source_products=[],
    ))
    seeded.flush()

    report = reconcile_vanished_products(seeded, apply=True)
    seeded.flush()

    assert [r["product_id"] for r in report["skipped"]] == ["empty-shell"]
    assert seeded.get(Product, "empty-shell") is not None
