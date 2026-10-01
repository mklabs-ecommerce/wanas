"""`manage.py reset-data`: conversations, customers and bot orders go;
the catalogue, fees, settings, staff and Shopify stay."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from domain.db import session_scope
from domain.models import (
    ChannelIdentity,
    Client,
    Order,
    Product,
    SessionRow,
    ShippingRate,
    Staff,
    Variant,
)
from domain.services import carts, ids, orders, staff_admin
from manage import main

CUSTOMER = "201555000111"


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def _place(seeded):
    carts.add(seeded, "whatsapp", CUSTOMER, "rehla-hoodie-s-olive", 1)
    result = orders.place_order(
        seeded, channel="whatsapp", external_id=CUSTOMER, customer_name="Hazem",
        governorate="Cairo", address="1 Test Street", contact_phone="01055566677",
    )
    assert "error" not in result, result
    seeded.add(SessionRow(channel="whatsapp", external_id=CUSTOMER, history=[{"role": "user", "content": "hi"}]))
    staff_admin.create(seeded, "owner", "owner-password-1", role="owner")
    seeded.commit()
    return result["order_id"]


def test_reset_refuses_without_confirm(cairo_rate, seeded):
    _place(seeded)
    assert main(["reset-data"]) == 2
    with session_scope() as db:
        assert _count(db, Order) == 1


def test_reset_wipes_the_bots_records_and_keeps_the_shop(cairo_rate, seeded, tmp_path, capsys):
    first = _place(seeded)
    assert first.endswith("-1001")
    products, variants = _count(seeded, Product), _count(seeded, Variant)
    fee = seeded.get(ShippingRate, "Cairo").fee
    seeded.close()

    assert main(["reset-data", "--confirm", "--backup-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "orders: 1 deleted" in out

    backup = next(tmp_path.glob("rehla-backup-*.json"))
    tables = json.loads(backup.read_text(encoding="utf-8"))["tables"]
    assert len(tables["orders"]) == 1 and tables["sessions"]

    with session_scope() as db:
        for model in (Order, Client, ChannelIdentity, SessionRow):
            assert _count(db, model) == 0, model.__tablename__
        assert _count(db, Product) == products and _count(db, Variant) == variants
        assert db.get(ShippingRate, "Cairo").fee == fee
        assert _count(db, Staff) == 1
        # Order numbering starts over.
        assert ids.next_order_id(db) == f"{ids.ORDER_PREFIX}-1001"
