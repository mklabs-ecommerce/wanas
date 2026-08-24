"""Shipping rate seed from `data/governorates.json`.

All 27, English keys and Arabic labels, `fee: null` throughout. The real
numbers come from the shop; until then an order for that governorate is
refused rather than shipped free.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from backend.models import ShippingRate
from config.settings import DATA_DIR

GOVERNORATES_PATH = DATA_DIR / "governorates.json"
EXPECTED_GOVERNORATES = 27


def load_governorates(path: Path | None = None) -> list[dict]:
    with open(path or GOVERNORATES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def import_governorates(session: Session, path: Path | None = None) -> dict:
    """Idempotent. An existing fee is never overwritten by the seed's null --
    re-seeding must not wipe rates the shop has already set."""
    rows = load_governorates(path)
    for raw in rows:
        rate = session.get(ShippingRate, raw["key"])
        if rate is None:
            rate = ShippingRate(governorate=raw["key"], fee=raw.get("fee"))
            session.add(rate)
        rate.label_ar = raw["label_ar"]
    session.flush()
    return {"governorates": len(rows)}
