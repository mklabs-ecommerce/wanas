"""The startup catalog auto-seed.

An unseeded production database is a silent, total failure: the process is
healthy, `/health` says everything is configured, and every customer search
still comes back "we don't have that" -- because nobody ran the documented
`python manage.py seed` step against it. `app._ensure_catalog_seeded`
recovers from exactly that, and only that: on an already-seeded database it
must not touch a single row.
"""

from __future__ import annotations

from decimal import Decimal

from app import _ensure_catalog_seeded, _ensure_shipping_fees_set
from domain.models import Product, ShippingRate, Variant


def test_an_empty_catalog_is_seeded_automatically(db):
    assert db.query(Product).count() == 0
    # `domain.db` opens every SQLite transaction with BEGIN IMMEDIATE, so
    # even that read took a write lock; release it before the function under
    # test opens its own session, or the two deadlock (see
    # `tests/fake_shopify.py`'s `seed_from` for the same note).
    db.rollback()

    _ensure_catalog_seeded()

    assert db.query(Product).count() == 18
    assert db.query(Variant).count() == 208
    assert db.query(ShippingRate).count() == 27


def test_an_already_seeded_catalog_is_left_untouched(seeded):
    variant = seeded.query(Variant).first()
    variant.stock_qty = 999999
    seeded.commit()

    _ensure_catalog_seeded()

    seeded.expire_all()
    refreshed = seeded.get(Variant, variant.variant_id)
    assert refreshed.stock_qty == 999999


def test_governorates_with_no_fee_get_the_default(seeded):
    assert all(r.fee is None for r in seeded.query(ShippingRate).all())
    seeded.rollback()

    _ensure_shipping_fees_set()

    seeded.expire_all()
    fees = {r.fee for r in seeded.query(ShippingRate).all()}
    assert fees == {Decimal("110")}


def test_a_fee_staff_already_set_is_never_overwritten(seeded):
    cairo = seeded.get(ShippingRate, "Cairo")
    cairo.fee = Decimal("75")
    seeded.commit()

    _ensure_shipping_fees_set()

    seeded.expire_all()
    assert seeded.get(ShippingRate, "Cairo").fee == Decimal("75")
    others = [r.fee for r in seeded.query(ShippingRate).all() if r.governorate != "Cairo"]
    assert all(fee == Decimal("110") for fee in others)
