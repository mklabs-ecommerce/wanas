"""Staff-toggled overrides for a handful of named feature flags.

`backend/config.py`'s `Settings` stays env-only and immutable -- that
boundary is deliberate and does not move here. This is a small, separate
overlay on top of it: a `RuntimeSetting` row wins when present, the env
default (`settings.X`) wins when it is absent. Exactly the shape
`backend/services/catalog.py::_overlay` already uses for Shopify's price
over wanas.db's -- nothing structurally new, just a second thing worth
overriding without a redeploy.

Only three keys are ever meaningful; `KNOWN_FLAGS` is the whole vocabulary; a
random key is out of scope, and this module makes no attempt to support one.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import RuntimeSetting, utcnow


@dataclass(frozen=True)
class FlagInfo:
    key: str
    label_ar: str
    description_ar: str


#: Order is display order in the dashboard settings panel.
KNOWN_FLAGS: tuple[FlagInfo, ...] = (
    FlagInfo(
        "voice_notes_enabled",
        "الرسايل الصوتية",
        "لو مقفولة، كل رسالة صوتية تتحول لحد من الفريق من غير ما البوت يحاول يسمعها.",
    ),
    FlagInfo(
        "image_understanding_enabled",
        "فهم الصور",
        "لو مقفولة، كل صورة تتحول لحد من الفريق من غير ما البوت يحاول يشوفها.",
    ),
    FlagInfo(
        "interactive_messages_enabled",
        "القوايم التفاعلية",
        "لو مقفولة، البوت يسأل عن المحافظة بالكلام العادي بدل القايمة القابلة للمس.",
    ),
)

_KNOWN_KEYS = {flag.key for flag in KNOWN_FLAGS}


def get(session: Session, key: str, default: bool) -> bool:
    """The effective value for `key`: a staff override if one exists, else
    `default` (always the env-derived `settings.X` at the call site)."""
    row = session.get(RuntimeSetting, key)
    return default if row is None else row.value


def get_all(session: Session) -> dict[str, RuntimeSetting]:
    rows = session.scalars(select(RuntimeSetting)).all()
    return {row.key: row for row in rows}


def set(session: Session, key: str, value: bool, staff_id: int) -> RuntimeSetting:
    if key not in _KNOWN_KEYS:
        raise ValueError(f"unknown runtime flag {key!r}")
    row = session.get(RuntimeSetting, key)
    if row is None:
        row = RuntimeSetting(key=key, value=value)
        session.add(row)
    row.value = value
    row.updated_at = utcnow()
    row.updated_by = staff_id
    session.flush()
    return row
