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

# ---------------------------------------------------------------------------
# REHLA -- THE ONE PLACE TO EDIT THE PUBLISHED FACTS (placeholders for now)
# ---------------------------------------------------------------------------
#: Shipping fee per governorate key (keys from data/governorates.json). Any
#: governorate not listed pays DEFAULT_SHIPPING_FEE. These only ever *fill a
#: blank* in the rate table at boot (`app._ensure_shipping_fees_set`); once a
#: fee is stored, change it from the dashboard or `manage.py set-fee`.
SHIPPING_FEES: dict[str, Decimal] = {
    "Cairo": Decimal("70"),
    "Giza": Decimal("70"),
}
DEFAULT_SHIPPING_FEE = Decimal("85")

#: The published delivery promise, in days: "من 3 لـ 5 أيام".
DELIVERY_DAYS_MIN = 3
DELIVERY_DAYS = 5

#: Cash on delivery only.
PAYMENT_LINE = "الدفع كاش عند الاستلام."

#: Rehla's return & exchange policy:
#: * exchange within 14 days of delivery -- unused, original condition, tags
#:   and packaging -- for a size issue or a manufacturing defect;
#: * return within 7 days of delivery, shipping deducted from the refund; for
#:   a manufacturing defect Rehla covers all shipping;
#: * never: used or washed, no tags or packaging, sale/discounted items
#:   (except a manufacturing defect);
#: * a request is handed to the team with the order number (`request_human`).
EXCHANGE_DAYS = 14
RETURN_DAYS = 7
# ---------------------------------------------------------------------------


def fee_for(governorate: str) -> Decimal:
    """The published fee for one governorate key."""
    return SHIPPING_FEES.get(governorate, DEFAULT_SHIPPING_FEE)


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
    by_fee = _governorates_by_fee(session)
    fees = sorted(by_fee)
    if len(fees) == 1:
        return f"{_egp(fees[0])} جنيه لكل محافظات مصر"
    if len(fees) == 2 and len(by_fee[fees[0]]) <= 3:
        cheap = " وال".join(by_fee[fees[0]])
        return f"{_egp(fees[0])} جنيه لل{cheap}، و{_egp(fees[1])} جنيه لباقي المحافظات"
    return f"من {_egp(fees[0])} لـ {_egp(fees[-1])} جنيه حسب المحافظة"


_LABELS_AR = {"Cairo": "قاهرة", "Giza": "جيزة", "Alexandria": "إسكندرية"}


def _governorates_by_fee(session: Session | None) -> dict[Decimal, list[str]]:
    """Fee -> the governorates charged it (Arabic, article-less), from the
    rate table when there is one, else from the published constants."""
    pairs: list[tuple[str, str, Decimal]] = []
    if session is not None:
        from domain.models import ShippingRate

        for rate in session.query(ShippingRate).filter(ShippingRate.fee.is_not(None)).all():
            pairs.append((rate.governorate, rate.label_ar or rate.governorate, Decimal(str(rate.fee))))
    if not pairs:
        pairs = [(key, key, fee) for key, fee in SHIPPING_FEES.items()]
        pairs.append(("*", "*", DEFAULT_SHIPPING_FEE))
    out: dict[Decimal, list[str]] = {}
    for key, label, fee in pairs:
        name = _LABELS_AR.get(key) or (label[2:] if label.startswith("ال") else label)
        out.setdefault(fee, []).append(name)
    return out


def example_fee(session: Session | None = None) -> str:
    """A fee the shop really charges, for the prompt's layout examples -- so an
    example the model copies is never a number the shop does not use."""
    fees = shipping_fees(session) or sorted({*SHIPPING_FEES.values(), DEFAULT_SHIPPING_FEE})
    return _egp(fees[0])


def delivery_line() -> str:
    return f"من {DELIVERY_DAYS_MIN} لـ {DELIVERY_DAYS} أيام"


def return_eligibility(
    days_since_delivery: float | None,
    *,
    defect: bool = False,
    discounted: bool = False,
    used_or_washed: bool = False,
    tags_and_packaging: bool = True,
) -> dict:
    """What the policy above allows for one item, decided in code.

    `None` days means delivery was never recorded: the windows are "unknown",
    never rounded down to closed. A defect overrides the discount exclusion
    and moves every shipping cost onto Rehla; it does not make a used or
    washed item returnable, since that is the condition the defect is judged in.
    """
    blocked = used_or_washed or not tags_and_packaging or (discounted and not defect)

    def window(days: int) -> bool | str:
        if blocked:
            return False
        if days_since_delivery is None:
            return "unknown"
        return days_since_delivery <= days

    return {
        "exchange": window(EXCHANGE_DAYS),
        "return": window(RETURN_DAYS),
        "shipping_paid_by": "rehla" if defect else "customer",
        "refund_deducts_shipping": not defect,
        "next_step": "ask_order_number_then_request_human",
    }
