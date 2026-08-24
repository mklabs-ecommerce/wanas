"""Orders confirm on the channel they were placed on (STEP 9).

An Instagram order's confirmation must go down the Instagram DM -- the one
thread that is guaranteed to exist -- never into a WhatsApp conversation with
the checkout phone that may never have opened WhatsApp. The fallback for
orders placed before `Order.source_external_id` existed is the old behaviour,
unchanged.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from backend.config import settings
from backend.db import engine, session_scope
from backend.models import Channel, Client, Order
from backend.services import carts, identities, notifications, orders
from backend.services.reengagement import check_abandoned_carts, check_back_in_stock

IGSID = "98765432109876543"
PHONE = "01000000123"
VARIANT = "wanas-hoodie-s-olive"

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "migrate_add_order_source_external_id.py"
)

#: The re-engagement checks compare DB-read datetimes against an aware
#: `utcnow()`, which fails on this suite's throwaway SQLite (it hands back
#: naive datetimes) -- the same documented, PRE-EXISTING limitation behind
#: the seven standing failures in tests/test_reengagement.py, deliberately
#: left alone. The channel logic these tests pin is identical on either
#: database; run them against PostgreSQL via WANAS_TEST_DATABASE_URL.
needs_real_datetime_db = pytest.mark.skipif(
    engine.url.drivername.startswith("sqlite"),
    reason="pre-existing SQLite naive/aware datetime limitation (see OPENCODE_PROGRESS.md)",
)


def settings_idle_hours() -> float:
    return settings.abandoned_cart_hours


@pytest.fixture()
def senders():
    """A LogSender registered per channel; what each one recorded is asserted
    on, and the real registration state is restored afterwards."""
    ig = notifications.LogSender()
    wa = notifications.LogSender()
    notifications.register_sender(ig, channel=Channel.INSTAGRAM_DM.value)
    notifications.register_sender(wa, channel="whatsapp")
    yield ig, wa
    notifications.register_sender(notifications.LogSender(), channel="whatsapp")
    notifications._senders.pop(Channel.INSTAGRAM_DM.value, None)


def place_instagram_order(variant_id: str = VARIANT):
    with session_scope() as session:
        carts.add(session, Channel.INSTAGRAM_DM.value, IGSID, variant_id, 1)
    with session_scope() as session:
        result = orders.place_order(
            session,
            channel=Channel.INSTAGRAM_DM.value,
            external_id=IGSID,
            customer_name="Omar",
            governorate="Cairo",
            address="5 Test Street",
            contact_phone=PHONE,
        )
    assert result.get("reference"), result
    return result


# --- confirmations ---------------------------------------------------------


def test_an_instagram_order_confirms_in_the_dm_not_on_whatsapp(seeded, cairo_rate, senders):
    ig, wa = senders
    place_instagram_order()

    assert [m.to for m in ig.sent] == [IGSID]
    # Not a single message went to the phone over WhatsApp.
    assert [m.to for m in wa.sent if m.template == "order_confirmation"] == []


def test_the_order_remembers_which_identity_placed_it(seeded, cairo_rate, senders):
    place_instagram_order()

    with session_scope() as session:
        order = session.query(Order).order_by(Order.order_id.desc()).first()
        assert order.source_channel == Channel.INSTAGRAM_DM.value
        assert order.source_external_id == IGSID


def test_an_order_with_no_recorded_identity_falls_back_to_the_phone(
    seeded, cairo_rate, senders
):
    """The old behaviour, unchanged: a pre-`source_external_id` order has only
    the phone collected at checkout."""
    ig, wa = senders
    with session_scope() as session:
        client = Client(full_name="Old Customer", phone=PHONE)
        session.add(client)
        session.flush()
        order = Order(
            order_id="WNS-9001",
            client_id=client.client_id,
            source_channel="whatsapp",
            shipping_address="somewhere",
            contact_phone=PHONE,
            governorate="Cairo",
            subtotal=0,
            discount_amount=0,
            shipping_fee=60,
            total=60,
            status="Confirmed",
        )
        session.add(order)
        session.flush()
        notifications.order_confirmed(session, order)

    confirmation = [m for m in wa.sent if m.template == "order_confirmation"]
    assert [m.to for m in confirmation] == [PHONE]
    assert ig.sent == []


def test_status_and_feedback_pushes_follow_the_order_channel_too(seeded, cairo_rate, senders):
    ig, _wa = senders
    place_instagram_order()

    with session_scope() as session:
        order = session.query(Order).order_by(Order.order_id.desc()).first()
        notifications.order_status_changed(session, order, "Packed")

    packed = [m for m in ig.sent if m.template == "status_packed"]
    assert len(packed) == 1
    assert packed[0].to == IGSID


# --- re-engagement ----------------------------------------------------------


@needs_real_datetime_db
def test_an_abandoned_instagram_cart_is_nudged(seeded, cairo_rate, senders):
    from backend.models import CartItem, utcnow

    ig, wa = senders
    with session_scope() as session:
        identities.get_or_create(session, Channel.INSTAGRAM_DM.value, IGSID)
        session.add(
            CartItem(
                channel=Channel.INSTAGRAM_DM.value,
                external_id=IGSID,
                variant_id=VARIANT,
                quantity=1,
                added_at=utcnow() - timedelta(hours=settings_idle_hours() + 1),
            )
        )

    assert check_abandoned_carts() >= 1
    assert any(m.to == IGSID for m in ig.sent)


@needs_real_datetime_db
def test_check_back_in_stock_reads_the_channel_off_the_waitlist_row(seeded, senders):
    """Plan: 'check_back_in_stock already reads the channel off the waitlist
    row -- verify with a test, change nothing.'"""
    from backend.models import StockWaitlistEntry, Variant, utcnow

    ig, _wa = senders
    with session_scope() as session:
        identities.get_or_create(session, Channel.INSTAGRAM_DM.value, IGSID)
        variant = session.get(Variant, VARIANT)
        assert variant is not None and variant.stock_qty > 0
        session.add(
            StockWaitlistEntry(
                variant_id=VARIANT,
                channel=Channel.INSTAGRAM_DM.value,
                external_id=IGSID,
                requested_at=utcnow(),
            )
        )

    assert check_back_in_stock() == 1
    assert any(m.to == IGSID for m in ig.sent)


# --- identity matching at checkout -------------------------------------------


def test_checkout_on_instagram_still_offers_a_pending_link_for_a_known_phone(
    seeded, cairo_rate
):
    """`detect_pending_link_from_external_id` deliberately ignores an IGSID --
    it is not a phone number. But the customer *types* a phone at checkout, so
    the match-and-ask path has to run there regardless of channel."""
    with session_scope() as session:
        session.add(Client(full_name="Existing Customer", phone=PHONE))

    place_instagram_order()

    with session_scope() as session:
        identity = identities.get(session, Channel.INSTAGRAM_DM.value, IGSID)
        assert identity.pending_link is not None
        assert identity.pending_link["matched_on"] == "phone"


# --- the migration script -----------------------------------------------------


def _create_legacy_db(path: Path) -> None:
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (order_id VARCHAR(20) PRIMARY KEY, source_channel VARCHAR(20))")
    con.execute("INSERT INTO orders VALUES ('WNS-1001', 'whatsapp')")
    con.commit()
    con.close()


def _run_migration(db_path: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MIGRATION), "--db", str(db_path), *flags],
        capture_output=True,
        text=True,
    )


def test_the_migration_is_a_dry_run_by_default_and_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    _create_legacy_db(db)

    dry = _run_migration(db)
    assert dry.returncode == 0
    assert "Would add" in dry.stdout
    # Dry run wrote nothing.
    import sqlite3

    with sqlite3.connect(db) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
    assert "source_external_id" not in columns

    applied = _run_migration(db, "--apply")
    assert applied.returncode == 0
    assert "added orders.source_external_id" in applied.stdout

    again = _run_migration(db, "--apply")
    assert again.returncode == 0
    assert "Nothing to do" in again.stdout

    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT order_id, source_external_id FROM orders").fetchall()
    assert rows == [("WNS-1001", None)]
