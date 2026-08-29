"""What a customer's orders add up to, computed from the orders themselves.

The Customers screen used to read `numberOfOrders` and `amountSpent` straight
off the Shopify customer record. Those are one number each, and the shop owner
asked for four: how many orders stand, what they came to, how many were
*cancelled*, and what those came to. Shopify does not break its two totals
down that way, and a cancelled sale counted as revenue is the one error a
statistics screen must not make.

They also do not exist at all for a customer Shopify has never heard of -- a
bot buyer whose order was placed before the bot attached a customer to it --
and they are blank on every record `scripts/shopify_backfill_customers.py`
created, which is why those customers showed no governorate while the very
same people had one on the bot tab.

So all four numbers, the channels, and the governorate are folded out of the
order list instead. One walk, one vocabulary, and the Customers screen and the
Orders screen can no longer disagree: this reads the same
`admin_orders.order_summary` dicts the Orders table is drawn from.

Keyed by *both* the Shopify customer id and the normalised phone. An order
placed before customers were attached has no customer id but does have the
buyer's phone on its shipping address, and that is the only thread tying it to
the person -- see `integrations/shopify/orders.py::normalise_phone`, which is
the same normalisation the order path itself uses so `01067177128` and
`+201067177128` are one person rather than two rows.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from integrations.shopify import orders as shopify_orders

#: The order the channel chips are shown in, so two customers with the same
#: two channels never render them in a different order.
CHANNEL_ORDER = ("whatsapp", "instagram_dm", "web")


def phone_key(raw: str | None) -> str | None:
    """One phone in one shape, for matching a person across two systems.

    Falls back to the digits when `normalise_phone` refuses -- it only accepts
    Egyptian mobiles, and two rows carrying the same unrecognised number are
    still the same person for the purpose of not listing them twice.
    """
    if not raw:
        return None
    return shopify_orders.normalise_phone(raw) or "".join(c for c in raw if c.isdigit()) or None


def _amount(raw) -> Decimal:
    try:
        return Decimal(str(raw or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def blank() -> dict:
    return {
        "order_count": 0,
        "amount_spent": Decimal("0"),
        "cancelled_count": 0,
        "cancelled_amount": Decimal("0"),
        "channels": [],
        "governorate": None,
        "last_order_at": None,
    }


def fold(stats: dict, order: dict) -> dict:
    """Add one order to a running total. `order` is an `order_summary` dict.

    A cancelled order counts in exactly one pair of numbers, never in both.
    "Orders" on this screen means orders that still stand -- the owner asked
    for it in those words, and it is also the only reading under which
    `orders x average` is a number anyone can act on.
    """
    total = _amount(order.get("total"))
    if order.get("cancelled"):
        stats["cancelled_count"] += 1
        stats["cancelled_amount"] += total
    else:
        stats["order_count"] += 1
        stats["amount_spent"] += total

    channel = order.get("channel") or order.get("channel_hint")
    if channel and channel not in stats["channels"]:
        stats["channels"].append(channel)

    # The most recent order that carried an address wins. A customer who moved
    # is where they last shipped to, and an older order must not overwrite it.
    created = order.get("created_at") or ""
    if order.get("governorate") and (
        stats["governorate"] is None or created >= (stats["last_order_at"] or "")
    ):
        stats["governorate"] = order["governorate"]
    if created > (stats["last_order_at"] or ""):
        stats["last_order_at"] = created
    return stats


def _finish(stats: dict) -> dict:
    stats["channels"] = [c for c in CHANNEL_ORDER if c in stats["channels"]] + [
        c for c in stats["channels"] if c not in CHANNEL_ORDER
    ]
    stats["amount_spent"] = f"{stats['amount_spent']:.2f}"
    stats["cancelled_amount"] = f"{stats['cancelled_amount']:.2f}"
    return stats


def summarise(orders: list[dict]) -> dict:
    """One person's orders, folded. Amounts come back as strings, the way
    every other money field crosses this API."""
    stats = blank()
    for order in orders:
        fold(stats, order)
    return _finish(stats)


def index(orders: list[dict]) -> dict[str, dict]:
    """Every person in `orders`, keyed by Shopify customer id *and* by phone.

    One dict object is shared by both keys for the same person, so a lookup by
    either finds the same totals. That is what lets an order with a customer
    record and an order without one -- same buyer, same phone -- count once
    each into one row on the screen.
    """
    by_key: dict[str, dict] = {}
    for order in orders:
        keys = [k for k in (order.get("customer_gid"), phone_key(order.get("customer_phone"))) if k]
        if not keys:
            continue
        stats = next((by_key[k] for k in keys if k in by_key), None) or blank()
        fold(stats, order)
        for key in keys:
            by_key[key] = stats
    # One person is one dict under two keys, so finishing per *key* would
    # format the same totals twice and turn a Decimal into a string it then
    # tried to format again. Finished once per object, by identity.
    for stats in {id(s): s for s in by_key.values()}.values():
        _finish(stats)
    return by_key


def merge_into(customer: dict, stats: dict | None) -> dict:
    """Put the four numbers, the channels and the governorate on a customer row.

    The customer's own `governorate` wins when it has one -- a Shopify record
    with a default address is stating where that person is, while the ledger's
    is inferred from where they last had something shipped.
    """
    stats = stats or _finish(blank())
    customer["order_count"] = stats["order_count"]
    customer["amount_spent"] = stats["amount_spent"]
    customer["cancelled_count"] = stats["cancelled_count"]
    customer["cancelled_amount"] = stats["cancelled_amount"]
    customer["channels"] = list(stats["channels"])
    customer["last_order_at"] = stats["last_order_at"]
    if not customer.get("governorate"):
        customer["governorate"] = stats["governorate"]
    return customer


#: Which segment tab a customer belongs on.
SEGMENTS = ("all", "bot", "web")
BOT_CHANNELS = ("whatsapp", "instagram_dm")


def in_segment(customer: dict, segment: str) -> bool:
    """The three tabs are three views of one list, not three lists.

    A customer who bought once in a conversation and once on the website is on
    both tabs, which is the honest answer -- the alternative is picking one
    channel per person and being wrong about the other sale.

    A bot customer with no orders yet is still a bot customer: they exist in
    wanas.db because a conversation created them, which nothing on the website
    does.
    """
    if segment == "all":
        return True
    channels = customer.get("channels") or []
    if segment == "bot":
        return any(c in BOT_CHANNELS for c in channels) or customer.get("source") == "bot"
    return "web" in channels
