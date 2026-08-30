"""What else has to let go when a product stops being sellable.

Deleting a product used to mean deleting its rows and its Shopify product,
and stopping there. Three other places hold something that only makes sense
while the variant exists, and each goes wrong quietly:

- a cart holding it fails at checkout, the most expensive place to fail;
- someone waits forever for a back-in-stock message that can never come;
- a staff member approves an `item_swap` into a size that is not there.

Archiving is the worse half of that, and the reason `release_variants` takes
a `gone` flag rather than being folded into the delete: an archived variant
still exists, so nothing errors -- the waiting simply never ends.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from domain.models import (
    CartItem,
    Client,
    Order,
    OrderItem,
    Product,
    QueueKind,
    QueueStatus,
    SizeChart,
    StaffQueueItem,
    StockWaitlistEntry,
)
from integrations.shopify import admin_products as sap

CHANNEL = "whatsapp"
WHO = "201000000000"


def _product(session, title="Sync Tee", variants=None):
    return sap.create_product(
        session,
        title=title,
        description="",
        category="T-Shirts",
        department="unisex",
        style=None,
        collection=None,
        size_chart=None,
        variants=variants or [
            {"size": "S", "color": "Olive", "price": 300, "stock_qty": 1},
            {"size": "M", "color": "Olive", "price": 300, "stock_qty": 1},
        ],
    )


def _waiting_on(session, variant_id):
    session.add(CartItem(channel=CHANNEL, external_id=WHO, variant_id=variant_id, quantity=1))
    session.add(StockWaitlistEntry(
        channel=CHANNEL, external_id=WHO, variant_id=variant_id, observed_stock=0
    ))
    session.flush()


def _swap_request(session, to_variant_id, queue_id="Q-1"):
    session.add(StaffQueueItem(
        queue_id=queue_id,
        kind=QueueKind.ITEM_SWAP.value,
        status=QueueStatus.OPEN.value,
        channel=CHANNEL,
        external_id=WHO,
        summary="WNS-1: swap Sync Tee (Olive, S)",
        payload={"channel": CHANNEL, "external_id": WHO,
                 "from_variant_id": "other-variant", "to_variant_id": to_variant_id},
    ))
    session.flush()
    return queue_id


def _sold(session, variant_id, order_id="WNS-SYNC-1"):
    client = Client(full_name="Sara", phone="201000000001", address="somewhere")
    session.add(client)
    session.flush()
    session.add(Order(
        order_id=order_id, client_id=client.client_id, source_channel=CHANNEL,
        shipping_address="somewhere", contact_phone="201000000001", governorate="Cairo",
        subtotal=Decimal("300"), shipping_fee=Decimal("60"), total=Decimal("360"),
        status="Confirmed",
    ))
    session.add(OrderItem(
        order_id=order_id, variant_id=variant_id, product_name="Sync Tee",
        size="S", color="Olive", quantity=1,
        unit_price=Decimal("300"), unit_original_price=Decimal("300"),
    ))
    session.flush()


def _open_swap(session, queue_id="Q-1"):
    return session.get(StaffQueueItem, queue_id)


# --------------------------------------------------------------------------
# deleting
# --------------------------------------------------------------------------


def test_deleting_a_product_empties_the_carts_holding_it(seeded, shopify):
    result = _product(seeded)
    _waiting_on(seeded, "sync-tee-s-olive")

    out = sap.delete_product(seeded, result["product_id"])

    assert out["released"]["carts"] == 1
    assert seeded.scalars(select(CartItem)).all() == []


def test_deleting_a_product_ends_the_waiting_for_it_to_come_back(seeded, shopify):
    result = _product(seeded)
    _waiting_on(seeded, "sync-tee-s-olive")

    sap.delete_product(seeded, result["product_id"])

    assert seeded.scalars(select(StockWaitlistEntry)).all() == []


def test_deleting_a_size_takes_the_dead_swap_target_off_the_queue(seeded, shopify):
    """The request stays open -- the customer still wants a different size.
    What changed is that the one named is not available, which is a person's
    call, not this function's."""
    _product(seeded)
    _swap_request(seeded, "sync-tee-m-olive")

    sap.delete_variant(seeded, "sync-tee-m-olive")

    item = _open_swap(seeded)
    assert item.status == QueueStatus.OPEN.value
    assert item.payload["to_variant_id"] is None
    assert item.payload["unavailable_target"] == "sync-tee-m-olive"
    assert "no longer sold" in item.summary


def test_a_swap_pointing_somewhere_else_is_untouched(seeded, shopify):
    _product(seeded)
    _swap_request(seeded, "wanas-hoodie-s-olive")

    sap.delete_variant(seeded, "sync-tee-m-olive")

    assert _open_swap(seeded).payload["to_variant_id"] == "wanas-hoodie-s-olive"


def test_a_swap_already_dealt_with_is_left_alone(seeded, shopify):
    """Only open requests. A resolved one is a record of what was done."""
    _product(seeded)
    _swap_request(seeded, "sync-tee-m-olive")
    _open_swap(seeded).status = QueueStatus.RESOLVED.value
    seeded.flush()

    sap.delete_variant(seeded, "sync-tee-m-olive")

    assert _open_swap(seeded).payload["to_variant_id"] == "sync-tee-m-olive"


# --------------------------------------------------------------------------
# archiving -- the half that fails silently
# --------------------------------------------------------------------------


def test_archiving_ends_the_waiting_too(seeded, shopify):
    """The variant still exists, so nothing errors. That is exactly the
    problem: the customer waits forever for a product that is not coming."""
    _product(seeded)
    _sold(seeded, "sync-tee-s-olive")
    _waiting_on(seeded, "sync-tee-s-olive")

    out = sap.archive_product(seeded, "sync-tee")

    assert out["released"]["waitlists"] == 1
    assert seeded.scalars(select(StockWaitlistEntry)).all() == []
    assert seeded.scalars(select(CartItem)).all() == []


def test_archiving_says_archived_not_no_longer_sold(seeded, shopify):
    _product(seeded)
    _sold(seeded, "sync-tee-s-olive")
    _swap_request(seeded, "sync-tee-m-olive")

    sap.archive_product(seeded, "sync-tee")

    assert "archived" in _open_swap(seeded).summary


def test_archiving_keeps_the_order_that_sold_it(seeded, shopify):
    _product(seeded)
    _sold(seeded, "sync-tee-s-olive")

    sap.archive_product(seeded, "sync-tee")

    assert seeded.scalars(
        select(OrderItem).where(OrderItem.variant_id == "sync-tee-s-olive")
    ).all() != []


# --------------------------------------------------------------------------
# the size chart the product was the last user of
# --------------------------------------------------------------------------


def test_a_dashboard_chart_nothing_uses_any_more_goes_with_it(seeded, shopify):
    result = _product(seeded)
    seeded.add(SizeChart(chart_id="sync-tee", title="Sync Tee", unit="cm",
                         measurements=[{"key": "width", "label_en": "Width", "label_ar": "العرض"}],
                         sizes={"S": {"width": 54}}))
    seeded.get(Product, result["product_id"]).size_chart = "sync-tee"
    seeded.flush()

    out = sap.delete_product(seeded, result["product_id"])

    assert out["chart_deleted"] == "sync-tee"
    assert seeded.get(SizeChart, "sync-tee") is None


def test_a_chart_another_product_still_uses_stays(seeded, shopify):
    """Charts are deliberately shared -- every boxy tee uses one. "Its product
    went" and "it is unused" are different statements."""
    first = _product(seeded, title="Sync Tee")
    second = _product(seeded, title="Other Tee")
    seeded.add(SizeChart(chart_id="shared-chart", title="Shared", unit="cm",
                         measurements=[{"key": "width", "label_en": "Width", "label_ar": "العرض"}],
                         sizes={"S": {"width": 54}}))
    seeded.get(Product, first["product_id"]).size_chart = "shared-chart"
    seeded.get(Product, second["product_id"]).size_chart = "shared-chart"
    seeded.flush()

    out = sap.delete_product(seeded, first["product_id"])

    assert out["chart_deleted"] is None
    assert seeded.get(SizeChart, "shared-chart") is not None


def test_a_chart_from_the_json_file_is_never_deleted(seeded, shopify):
    """Those ship with the code and are not this function's to remove."""
    result = _product(seeded)
    seeded.get(Product, result["product_id"]).size_chart = "ringer-boxy-tee"
    seeded.flush()

    out = sap.delete_product(seeded, result["product_id"])

    assert out["chart_deleted"] is None


# --------------------------------------------------------------------------
# the boot-time report
# --------------------------------------------------------------------------


def test_the_boot_report_writes_nothing(seeded, shopify, monkeypatch, caplog):
    """It runs unattended on every deploy. A reconcile that deletes unattended
    is one bad Shopify read away from an empty catalog."""
    import app as app_module
    from integrations.shopify import product_reconcile

    result = _product(seeded)
    shopify.shopify_delete_product(result["shopify_id"])
    seeded.commit()

    calls = []
    real = product_reconcile.reconcile_vanished_products
    monkeypatch.setattr(
        product_reconcile, "reconcile_vanished_products",
        lambda session, **kw: (calls.append(kw), real(session, **kw))[1],
    )
    with caplog.at_level("WARNING"):
        app_module._report_vanished_products()

    assert calls and calls[0]["apply"] is False
    assert seeded.get(Product, result["product_id"]) is not None
    assert result["product_id"] in caplog.text


def test_the_boot_report_is_quiet_when_the_two_sides_agree(seeded, shopify, caplog):
    import app as app_module

    seeded.commit()
    with caplog.at_level("WARNING"):
        app_module._report_vanished_products()

    assert "no longer on Shopify" not in caplog.text


def test_a_refused_read_is_a_warning_not_a_crash(seeded, shopify, monkeypatch, caplog):
    """Boot must survive it, like every other optional startup step."""
    import app as app_module

    monkeypatch.setattr(sap, "all_variant_skus", set)
    with caplog.at_level("WARNING"):
        app_module._report_vanished_products()

    assert "could not run" in caplog.text


def test_an_outage_does_not_take_the_boot_down(seeded, shopify, caplog):
    import app as app_module

    shopify.down = True
    with caplog.at_level("WARNING"):
        app_module._report_vanished_products()  # must not raise
