"""The facts the shop publishes without a lookup, each written down once.

Three sentences are said to customers with no tool call behind them: what
shipping costs, how long delivery takes, and how to pay. They were written
out as literals in three places -- the system prompt, the public comment
answers (`assistant/comment_faq.py`) and, for the fee, parsed back out of the
comment answer by `app.py` to seed the rate table -- and the rate table itself
is what `get_shipping_fee` and every order actually charge. A fee changed in
the dashboard changed the orders and left the bot quoting the old number in
every conversation that asked.

So the fee is read from the rate table, the constants live here, and every
surface renders its sentence from this module. The model is handed the
result, never asked to remember it.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from common.money import money

#: The flat rate the shop set for every governorate on 2026-08-20, confirmed
#: across ~100 completed orders. Only ever *fills a blank*: `app.py` sets it
#: on a governorate with no fee yet, and a fee staff set in the dashboard is
#: never overwritten. What the bot says is read from the table, not from this.
DEFAULT_SHIPPING_FEE = Decimal("110")

#: The published delivery promise, in days, to every governorate.
DELIVERY_DAYS = 4

#: The two ways this shop can be paid, in the one sentence it says them in.
#: `scripts/quality_gate.py` fails any reply offering a third.
PAYMENT_LINE = "بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع."


def shipping_fees(session: Session | None) -> list[Decimal]:
    """Every distinct fee the rate table holds, lowest first. Empty without
    a session or before any fee is set."""
    if session is None:
        return []
    from domain.models import ShippingRate

    fees = {
        Decimal(str(fee))
        for (fee,) in session.query(ShippingRate.fee).filter(ShippingRate.fee.is_not(None)).all()
    }
    return sorted(fees)


def _egp(value: Decimal) -> str:
    amount = money(value)
    return str(int(amount)) if float(amount).is_integer() else str(amount)


def shipping_line(session: Session | None = None) -> str:
    """What shipping costs, as the shop says it.

    One fee everywhere is "<fee> جنيه لكل محافظات مصر" -- the sentence the
    shop has always published. Different fees are a range and a pointer to
    the governorate, because quoting one of them as the price is quoting the
    wrong price to everyone else. Without a table to read, the default.
    """
    fees = shipping_fees(session) or [DEFAULT_SHIPPING_FEE]
    if len(fees) == 1:
        return f"{_egp(fees[0])} جنيه لكل محافظات مصر"
    return f"من {_egp(fees[0])} لـ {_egp(fees[-1])} جنيه حسب المحافظة"


def example_fee(session: Session | None = None) -> str:
    """A fee the shop really charges, for the prompt's layout examples -- so an
    example the model copies is never a number the shop does not use."""
    fees = shipping_fees(session) or [DEFAULT_SHIPPING_FEE]
    return _egp(fees[0])


def delivery_line() -> str:
    return f"بياخد لغاية {DELIVERY_DAYS} أيام"
