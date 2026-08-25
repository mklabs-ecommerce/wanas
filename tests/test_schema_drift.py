"""A column the models declare and the table does not have is named at boot.

This is the bug that took production down for four days: `orders` was created
before `source_external_id` existed, `create_all` adds tables and not columns,
and nothing said so. Every order was created on Shopify, failed its `INSERT`,
and was cancelled again -- while the service looked healthy.
"""

from __future__ import annotations

from sqlalchemy import text

from domain.db import SessionLocal, engine, session_scope
from domain.models import Base, Variant
from domain.schema_drift import add_column_sql, addable, apply_additive, detect, log_drift

VARIANT = "wanas-hoodie-s-olive"


def test_a_matching_schema_has_no_drift(db):
    assert detect(engine).clean


def test_a_dropped_column_is_detected_and_named(db):
    db.execute(text("ALTER TABLE orders DROP COLUMN source_external_id"))
    db.commit()
    try:
        drift = detect(engine)

        assert not drift.clean
        assert [c.name for c in drift.missing_columns["orders"]] == ["source_external_id"]
        assert any("orders.source_external_id" in line for line in drift.describe())
    finally:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)


def test_the_drift_is_logged_as_an_error_with_the_fix(db, caplog):
    db.execute(text("ALTER TABLE orders DROP COLUMN source_external_id"))
    db.commit()
    try:
        with caplog.at_level("ERROR", logger="wanas.schema"):
            drift = log_drift(engine)

        assert not drift.clean
        assert "orders.source_external_id" in caplog.text
        assert "scripts/migrate_schema.py" in caplog.text
    finally:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)


def test_the_generated_sql_puts_the_column_back(db):
    db.execute(text("ALTER TABLE orders DROP COLUMN source_external_id"))
    db.commit()
    try:
        column = detect(engine).missing_columns["orders"][0]
        assert addable(column), "nullable, so it can be added to a populated table"

        db.execute(text(add_column_sql(engine, "orders", column)))
        db.commit()

        assert detect(engine).clean
    finally:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)


def test_a_not_null_column_with_no_default_is_not_added_behind_your_back():
    """It cannot be: the database would need a value for every existing row.
    Reported instead, so a person decides what those rows should say."""
    orders = Base.metadata.tables["orders"]
    assert not addable(orders.c.shipping_address)
    assert addable(orders.c.source_external_id)


def test_apply_additive_puts_a_missing_column_back(db, caplog):
    """What startup does on a database that predates a model change."""
    db.execute(text("ALTER TABLE orders DROP COLUMN source_external_id"))
    db.commit()
    try:
        with caplog.at_level("WARNING", logger="wanas.schema"):
            ran = apply_additive(engine)

        assert len(ran) == 1 and "source_external_id" in ran[0]
        assert "added missing column orders.source_external_id" in caplog.text
        assert detect(engine).clean
        # Idempotent: the next boot has nothing to do.
        assert apply_additive(engine) == []
    finally:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)


def test_an_order_can_be_placed_again_once_the_column_is_back(cairo_rate, shopify):
    """The production failure, end to end: drop the column, watch the order be
    created on Shopify and cancelled again, repair, and place it for real."""
    from domain.services import carts, orders

    who = "201555777888"

    with SessionLocal() as session:
        session.get(Variant, VARIANT).stock_qty = 5
        session.commit()
    shopify.set(VARIANT, qty=5)

    with SessionLocal() as session:
        session.execute(text("ALTER TABLE orders DROP COLUMN source_external_id"))
        session.commit()

    try:
        with session_scope() as session:
            carts.add(session, "whatsapp", who, VARIANT, 1)
            broken = orders.place_order(
                session,
                channel="whatsapp",
                external_id=who,
                customer_name="Hazem",
                governorate="Cairo",
                address="8 Maadi",
                contact_phone="01067177129",
            )

        assert broken["error"] == "order_failed"
        assert broken["stage"] == "local_write"
        assert [o for o in shopify.orders.values() if not o["cancelled"]] == []
        # The refusal rolled back to the savepoint, not the whole transaction:
        # the customer still has the cart they built, so retrying is enough.
        with SessionLocal() as session:
            assert not carts.is_empty(session, "whatsapp", who)

        apply_additive(engine)

        with session_scope() as session:
            placed = orders.place_order(
                session,
                channel="whatsapp",
                external_id=who,
                customer_name="Hazem",
                governorate="Cairo",
                address="8 Maadi",
                contact_phone="01067177129",
            )

        assert "error" not in placed, placed
        assert placed["status"] == "Confirmed"
        assert len([o for o in shopify.orders.values() if not o["cancelled"]]) == 1
    finally:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
