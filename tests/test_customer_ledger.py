"""What a customer's orders add up to.

The Customers screen used to show `numberOfOrders` and `amountSpent` -- one
number each, both of them counting cancelled sales, and both blank on the
customer records the backfill created. The owner asked for four numbers and
the channels instead, so all of it is folded out of the orders.

The failure this guards is not a crash. It is a screen that quietly counts a
cancelled order as revenue, or lists one buyer as two people because one of
their orders predates customer records.
"""

from __future__ import annotations

from dashboard import customer_ledger as ledger


def order(**overrides):
    node = {
        "id": "gid://shopify/Order/1",
        "customer_gid": "gid://shopify/Customer/1",
        "customer_phone": "+201067177128",
        "total": "700.00",
        "cancelled": False,
        "channel": "whatsapp",
        "governorate": "Cairo",
        "created_at": "2026-08-01T00:00:00Z",
    }
    node.update(overrides)
    return node


# --------------------------------------------------------------------------
# cancelled is its own pair of numbers
# --------------------------------------------------------------------------


def test_a_cancelled_order_is_never_counted_as_revenue():
    """The one mistake this screen must not make. "Orders" means the orders
    that still stand -- the owner asked for it in those words."""
    stats = ledger.summarise([order(), order(total="500.00", cancelled=True)])

    assert stats["order_count"] == 1
    assert stats["amount_spent"] == "700.00"
    assert stats["cancelled_count"] == 1
    assert stats["cancelled_amount"] == "500.00"


def test_a_customer_whose_every_order_was_cancelled_is_not_a_customer_with_none():
    """Zero orders and zero spent, but the cancellations still show. A buyer
    who tried four times and cancelled four times is a fact staff need."""
    stats = ledger.summarise([order(cancelled=True), order(cancelled=True)])

    assert (stats["order_count"], stats["amount_spent"]) == (0, "0.00")
    assert (stats["cancelled_count"], stats["cancelled_amount"]) == (2, "1400.00")


def test_a_total_shopify_did_not_send_is_zero_rather_than_a_crash():
    assert ledger.summarise([order(total=None)])["amount_spent"] == "0.00"


# --------------------------------------------------------------------------
# one person, however their orders were recorded
# --------------------------------------------------------------------------


def test_an_order_with_no_customer_record_still_lands_on_the_right_person():
    """Every order the bot placed before it attached customers has no customer
    id -- only the phone on the shipping address. Keying on the id alone is
    how the same buyer became two rows, one of them with half their history."""
    index = ledger.index([
        order(id="a"),
        order(id="b", customer_gid=None, total="300.00"),
    ])

    by_id = index["gid://shopify/Customer/1"]
    by_phone = index[ledger.phone_key("+201067177128")]
    assert by_id is by_phone
    assert by_id["order_count"] == 2
    assert by_id["amount_spent"] == "1000.00"


def test_the_same_phone_written_two_ways_is_one_person():
    index = ledger.index([
        order(id="a", customer_gid=None, customer_phone="01067177128"),
        order(id="b", customer_gid=None, customer_phone="+201067177128", total="300.00"),
    ])

    assert len({id(v) for v in index.values()}) == 1
    assert next(iter(index.values()))["order_count"] == 2


def test_an_order_that_names_nobody_at_all_is_skipped_not_bucketed_together():
    """Two anonymous orders are not one anonymous customer."""
    index = ledger.index([order(customer_gid=None, customer_phone=None)])

    assert index == {}


def test_the_totals_are_formatted_once_even_though_two_keys_share_them():
    """One dict under two keys. Finishing per key formatted the same Decimal
    twice and blew up on the second pass."""
    index = ledger.index([order()])

    assert index["gid://shopify/Customer/1"]["amount_spent"] == "700.00"


# --------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------


def test_every_channel_a_customer_ever_ordered_through_is_kept():
    stats = ledger.summarise([
        order(channel="whatsapp"),
        order(channel="web"),
        order(channel="whatsapp"),
    ])

    assert stats["channels"] == ["whatsapp", "web"]


def test_the_channels_come_back_in_a_fixed_order():
    """Two customers with the same two channels must not render them in a
    different order because their orders arrived in a different one."""
    a = ledger.summarise([order(channel="web"), order(channel="whatsapp")])
    b = ledger.summarise([order(channel="whatsapp"), order(channel="web")])

    assert a["channels"] == b["channels"] == ["whatsapp", "web"]


def test_shopifys_own_reading_is_used_when_nothing_local_says_otherwise():
    """`channel` is what the local order row recorded; `channel_hint` is what
    Shopify's Channel column and tags say. An order with no local row has only
    the second, and it is still an answer."""
    stats = ledger.summarise([order(channel=None, channel_hint="instagram_dm")])

    assert stats["channels"] == ["instagram_dm"]


# --------------------------------------------------------------------------
# the governorate a customer record does not carry
# --------------------------------------------------------------------------


def test_the_governorate_is_taken_from_where_they_last_shipped():
    stats = ledger.summarise([
        order(governorate="Cairo", created_at="2026-01-01T00:00:00Z"),
        order(governorate="Giza", created_at="2026-08-01T00:00:00Z"),
    ])

    assert stats["governorate"] == "Giza"


def test_an_order_with_no_address_does_not_erase_a_governorate():
    stats = ledger.summarise([
        order(governorate="Cairo", created_at="2026-01-01T00:00:00Z"),
        order(governorate=None, created_at="2026-08-01T00:00:00Z"),
    ])

    assert stats["governorate"] == "Cairo"


def test_a_customers_own_governorate_beats_the_one_the_orders_imply():
    """A Shopify record with a default address is stating where the person is.
    The ledger's is inferred from a shipping address, which may have been a
    one-off delivery to somebody else."""
    row = ledger.merge_into({"governorate": "Alexandria"}, ledger.summarise([order()]))

    assert row["governorate"] == "Alexandria"


def test_a_customer_with_no_governorate_gets_the_one_the_orders_imply():
    """The reported bug: the same person had a governorate on the bot tab and
    a dash on the store tab, because every customer record the backfill
    created has no default address at all."""
    row = ledger.merge_into({"governorate": None}, ledger.summarise([order()]))

    assert row["governorate"] == "Cairo"


def test_a_customer_with_no_orders_at_all_reads_as_zeroes_not_as_missing():
    row = ledger.merge_into({"name": "Nobody"}, None)

    assert row["order_count"] == 0
    assert row["amount_spent"] == "0.00"
    assert row["cancelled_count"] == 0
    assert row["channels"] == []


# --------------------------------------------------------------------------
# the three tabs
# --------------------------------------------------------------------------


def test_every_customer_is_on_the_all_tab():
    assert ledger.in_segment({"channels": []}, "all")


def test_a_customer_who_bought_on_both_is_on_both_tabs():
    """Picking one channel per person means being wrong about the other sale."""
    customer = {"channels": ["whatsapp", "web"]}

    assert ledger.in_segment(customer, "bot")
    assert ledger.in_segment(customer, "web")


def test_an_instagram_buyer_is_a_bot_customer_not_a_website_one():
    customer = {"channels": ["instagram_dm"]}

    assert ledger.in_segment(customer, "bot")
    assert not ledger.in_segment(customer, "web")


def test_a_bot_customer_who_has_not_ordered_yet_is_still_a_bot_customer():
    """They exist in wanas.db because a conversation created them, which
    nothing on the website does."""
    assert ledger.in_segment({"channels": [], "source": "bot"}, "bot")
    assert not ledger.in_segment({"channels": [], "source": "bot"}, "web")
