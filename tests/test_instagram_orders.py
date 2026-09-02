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

from config.settings import settings
from domain.db import session_scope
from domain.models import Channel, Client, Order
from domain.services import (
    carts,
    identities,
    notifications,
    orders,
)
from domain.services.reengagement import check_abandoned_carts, check_back_in_stock

IGSID = "98765432109876543"
PHONE = "01000000123"
VARIANT = "wanas-hoodie-s-olive"

#: The general schema migrator. There used to be a second, single-column,
#: SQLite-only script beside it (`migrate_add_order_source_external_id.py`,
#: and a third for the Shopify order columns); they did a strict subset of
#: this one's job and were deleted. The guarantee the deleted script's test
#: pinned -- dry run by default, idempotent, additive-only -- is pinned here,
#: at the entry point that survived.
MIGRATION = Path(__file__).resolve().parent.parent / "scripts" / "migrate_schema.py"

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


def test_an_abandoned_instagram_cart_is_nudged(seeded, cairo_rate, senders):
    from domain.models import CartItem, utcnow

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


def test_check_back_in_stock_reads_the_channel_off_the_waitlist_row(seeded, senders):
    """Plan: 'check_back_in_stock already reads the channel off the waitlist
    row -- verify with a test, change nothing.'

    The entry is baselined at zero on purpose: a restock message describes a
    *verified transition*, so an entry with no `observed_stock` is baselined
    and left silent. This test is about which channel the notice goes out on,
    which needs a notice to go out at all."""
    from domain.models import StockWaitlistEntry, Variant, utcnow

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
                observed_stock=0,
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
    """A database in exactly the shape production was in: the full schema, one
    order in it, and `orders.source_external_id` missing.

    Built by creating the real schema and dropping the column, rather than by
    hand-writing a stub `orders` table -- a stub is *also* missing a dozen
    NOT NULL columns, which the migrator correctly refuses to guess at, and
    the refusal would be all this test ever exercised.
    """
    import sqlite3

    from sqlalchemy import create_engine

    from domain.models import Base

    legacy_engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(legacy_engine)
    legacy_engine.dispose()

    con = sqlite3.connect(path)
    con.execute("ALTER TABLE orders DROP COLUMN source_external_id")
    # One real row, so the migration is exercised against a *populated* table
    # -- that is the whole reason it may only add nullable columns. Every
    # NOT NULL column is filled with a placeholder; which values they carry
    # does not matter here, only that the row survives untouched.
    info = list(con.execute("PRAGMA table_info(orders)"))
    required = {
        row[1]: ("WNS-1001" if row[1] == "order_id" else 0 if "INT" in (row[2] or "").upper() else "x")
        for row in info
        if row[3] and row[4] is None
    }
    required["order_id"] = "WNS-1001"
    required["source_channel"] = "whatsapp"
    columns = ", ".join(required)
    placeholders = ", ".join("?" for _ in required)
    con.execute(f"INSERT INTO orders ({columns}) VALUES ({placeholders})", list(required.values()))
    con.commit()
    con.close()


def _run_migration(db_path: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MIGRATION), "--database-url", f"sqlite:///{db_path}", *flags],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )


def test_the_migration_is_a_dry_run_by_default_and_idempotent(tmp_path):
    """`source_external_id` is the column whose absence made every order
    create itself on Shopify and then cancel again, for four days. The
    migrator must report it without writing anything, add it on --apply, and
    say there is nothing to do on the run after that.

    It also has to *run at all*: invoked as `python scripts/migrate_schema.py`
    it put scripts/ on sys.path instead of the repo root and died on
    `import domain`, so the documented recovery step for that outage was
    itself broken.
    """
    db = tmp_path / "legacy.db"
    _create_legacy_db(db)

    dry = _run_migration(db)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "Would add" in dry.stdout
    assert "ALTER TABLE orders ADD COLUMN source_external_id" in dry.stdout
    # Dry run wrote nothing.
    import sqlite3

    with sqlite3.connect(db) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
    assert "source_external_id" not in columns

    applied = _run_migration(db, "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "added orders.source_external_id" in applied.stdout

    again = _run_migration(db, "--apply")
    assert again.returncode == 0
    assert "Nothing to do" in again.stdout

    # The existing order kept its data and reads NULL for the new column,
    # which is correct: it was placed before the bot recorded which identity
    # placed it.
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT order_id, source_external_id FROM orders").fetchall()
    assert rows == [("WNS-1001", None)]
