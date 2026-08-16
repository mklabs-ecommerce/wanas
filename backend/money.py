"""Money helpers.

Stored as an exact decimal, handed to the model and the templates as a plain
number in EGP -- never a formatted string (15-tool-contracts.md).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money(value) -> int | float:
    """Number, not a string. Whole amounts come back as ints so a reply reads
    "650" rather than "650.0"."""
    if value is None:
        return 0
    d = to_decimal(value)
    return int(d) if d == d.to_integral_value() else float(d)
