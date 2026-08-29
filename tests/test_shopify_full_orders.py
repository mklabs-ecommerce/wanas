"""The order now exists on Shopify, not only in wanas.db.

The bug this file mostly exists to prevent is the double decrement. Shopify
takes the stock as part of `orderCreate`, and the previous design also reserved
it by hand -- keeping both would quietly sell every item twice, and nothing
about the order or the reply would look wrong.

The rest is compensation: Shopify has no savepoint, so every path where the
local write does not land has to put the remote order back.
"""

from __future__ import annotations

import pytest

from domain.models import Order, ShippingRate
from domain.services import (
    carts,
    orders,
)
from integrations.shopify import orders as shopify_orders

VARIANT = "wanas-hoodie-s-olive"
OTHER = "wanas-hoodie-m-black"
WHO = "201555000222"


@pytest.fixture()
def priced(seeded):
    seeded.get(ShippingRate, "Cairo").fee = 60
    seeded.commit()
    return seeded


def place(session, **overrides):
    args = {
        "channel": "whatsapp",
        "external_id": WHO,
        "customer_name": "Hazem Abdelhamid",
        "governorate": "Cairo",
        "address": "12 El Sebki Street",
        "contact_phone": "01067177128",
    }
    args.update(overrides)
    return orders.place_order(session, **args)


# --------------------------------------------------------------------------
# the order reaches Shopify
# --------------------------------------------------------------------------


def test_a_sale_creates_an_order_on_shopify(priced, shopify):
    shopify.set(VARIANT, qty=5)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)

    result = place(priced)

    assert "order_id" in result, result
    assert len(shopify.orders) == 1
    remote = next(iter(shopify.orders.values()))
    assert remote["lines"] == {VARIANT: 2}
    assert remote["reference"] == f"whatsapp:{WHO}"


def test_the_shopify_number_is_stored_and_is_what_the_customer_is_told(priced, shopify):
    shopify.set(VARIANT, qty=5)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    result = place(priced)
    order = priced.get(Order, result["order_id"])

    assert order.shopify_order_id
    assert order.shopify_order_name.startswith("#")
    # `order_id` stays the internal handle every other tool takes; `reference`
    # is the only one meant to be said out loud.
    assert result["reference"] == order.shopify_order_name
    assert result["order_id"].startswith("WNS-")


def test_the_stock_is_taken_once_not_twice(priced, shopify):
    """The whole reason the manual reserve came out of this path."""
    shopify.set(VARIANT, qty=5)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)

    place(priced)

    assert shopify.qty(VARIANT) == 3
    assert shopify.reserved == [], "nothing should reserve stock alongside the order"


def test_the_local_row_follows_without_double_counting(priced, shopify):
    from domain.models import Variant

    shopify.set(VARIANT, qty=5)
    priced.get(Variant, VARIANT).stock_qty = 5
    priced.commit()
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)

    place(priced)

    assert priced.get(Variant, VARIANT).stock_qty == 3


# --------------------------------------------------------------------------
# the phone number
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed,sent",
    [
        ("01067177128", "+201067177128"),   # how every customer writes it
        ("0100 000 0000", "+201000000000"),  # with spaces
        ("201067177128", "+201067177128"),   # international, no plus
        ("+201067177128", "+201067177128"),  # already correct
        ("1067177128", "+201067177128"),     # leading zero lost
    ],
)
def test_local_numbers_are_translated_for_shopify(typed, sent):
    """Shopify checks the phone against the address country and rejects the
    whole order with "Phone is invalid" if it does not match. An Egyptian
    customer typing their own number the normal way was enough to lose every
    sale, so this is not cosmetic."""
    assert shopify_orders.normalise_phone(typed) == sent


@pytest.mark.parametrize("typed", ["12345", "", None, "0201067177128", "abc"])
def test_an_unreadable_number_is_dropped_not_guessed(typed):
    """A wrong number on a cash-on-delivery order is a parcel nobody can
    deliver. Better to leave the field empty -- it is still in the note and in
    wanas.db -- than to invent a plausible one."""
    assert shopify_orders.normalise_phone(typed) is None


def test_the_order_carries_the_number_shopify_will_accept(priced, shopify, monkeypatch):
    captured = {}
    real_create = shopify.create_order

    def spy(**kwargs):
        captured.update(kwargs)
        return real_create(**kwargs)

    monkeypatch.setattr(shopify_orders, "create_order", spy)
    shopify.set(VARIANT, qty=3)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    place(priced, contact_phone="01067177128")

    # The service is handed the number as the customer typed it; translating
    # is `create_order`'s job, so that every caller gets it right by default.
    assert captured["phone"] == "01067177128"


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_sold_out_is_refused_before_shopify_is_asked(priced, shopify):
    """Checked locally first so the reply can name the item. Shopify would
    refuse too, but its refusal cannot say which colour ran out."""
    shopify.set(VARIANT, qty=0)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    result = place(priced)

    assert result["error"] == "items_out_of_stock"
    assert result["items"][0]["color"] == "Olive"
    assert shopify.orders == {}


def test_an_outage_writes_no_order_anywhere(priced, shopify):
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)
    shopify.down = True

    result = place(priced)

    assert result["error"] == "store_unavailable"
    assert priced.query(Order).count() == 0
    assert shopify.orders == {}


def test_stock_lost_in_the_gap_is_refused_by_shopify_not_oversold(priced, shopify, monkeypatch):
    """Our check passes, then the storefront takes the last one before
    `orderCreate` runs. Shopify is the one that catches it."""
    shopify.set(VARIANT, qty=1)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)

    real_create = shopify.create_order

    def someone_else_first(**kwargs):
        shopify.shelf[VARIANT]["qty"] = 0
        return real_create(**kwargs)

    monkeypatch.setattr(shopify_orders, "create_order", someone_else_first)

    result = place(priced)

    assert result["error"] == "items_out_of_stock"
    assert priced.query(Order).count() == 0


def test_a_failed_local_write_cancels_the_shopify_order(priced, shopify, monkeypatch):
    """The compensating path. Without it every crash leaves a paid-looking
    order in the admin and its stock off the shelf."""
    shopify.set(VARIANT, qty=5)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)

    def boom(_session):
        raise RuntimeError("database went away")

    monkeypatch.setattr(orders, "next_order_id", boom)

    # It refuses rather than raising: a crash out of `confirm_order` ends the
    # customer's conversation with a generic apology, and the code that names
    # the failed stage is what a recurrence gets diagnosed from.
    result = place(priced)

    assert result["error"] == "order_failed"
    assert result["stage"] == "local_write"
    assert result["shopify_cancelled"] is True
    assert priced.query(Order).count() == 0
    remote = next(iter(shopify.orders.values()))
    assert remote["cancelled"] is True
    assert shopify.qty(VARIANT) == 5, "the cancel has to restock"


# --------------------------------------------------------------------------
# after the order
# --------------------------------------------------------------------------


def test_cancelling_cancels_on_shopify_and_restocks_once(priced, shopify):
    from domain.models import Variant

    shopify.set(VARIANT, qty=5)
    priced.get(Variant, VARIANT).stock_qty = 5
    priced.commit()
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)
    order_id = place(priced)["order_id"]

    orders.cancel(priced, priced.get(Order, order_id))

    remote = next(iter(shopify.orders.values()))
    assert remote["cancelled"] is True
    assert shopify.qty(VARIANT) == 5
    # Not 7: Shopify restocked, and the local row must not add them again.
    assert priced.get(Variant, VARIANT).stock_qty == 5


def test_reducing_a_quantity_edits_the_shopify_order(priced, shopify):
    shopify.set(VARIANT, qty=6)
    carts.add(priced, "whatsapp", WHO, VARIANT, 4)
    order_id = place(priced)["order_id"]
    assert shopify.qty(VARIANT) == 2

    orders.modify_quantity(priced, priced.get(Order, order_id), VARIANT, 1)

    remote = next(iter(shopify.orders.values()))
    assert remote["lines"][VARIANT] == 1
    assert shopify.qty(VARIANT) == 5


def test_increasing_beyond_the_shelf_is_refused_and_changes_nothing(priced, shopify):
    shopify.set(VARIANT, qty=3)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)
    order_id = place(priced)["order_id"]
    assert shopify.qty(VARIANT) == 1

    result = orders.modify_quantity(priced, priced.get(Order, order_id), VARIANT, 9)

    assert result["error"] == "insufficient_stock"
    remote = next(iter(shopify.orders.values()))
    assert remote["lines"][VARIANT] == 2
    assert shopify.qty(VARIANT) == 1


def test_an_outage_during_a_change_does_not_silently_diverge(priced, shopify):
    shopify.set(VARIANT, qty=6)
    carts.add(priced, "whatsapp", WHO, VARIANT, 3)
    order_id = place(priced)["order_id"]
    shopify.down = True

    result = orders.modify_quantity(priced, priced.get(Order, order_id), VARIANT, 1)

    assert result["error"] == "store_unavailable"
    order = priced.get(Order, order_id)
    assert order.items[0].quantity == 3, "the local order must not move on its own"


def test_a_swap_moves_the_line_on_shopify_too(priced, shopify):
    shopify.set(VARIANT, qty=4)
    shopify.set(OTHER, qty=4)
    carts.add(priced, "whatsapp", WHO, VARIANT, 1)
    order_id = place(priced)["order_id"]

    result = orders.apply_swap(priced, priced.get(Order, order_id), VARIANT, OTHER)

    assert "order_id" in result, result
    remote = next(iter(shopify.orders.values()))
    assert remote["lines"] == {OTHER: 1}
    assert shopify.qty(VARIANT) == 4
    assert shopify.qty(OTHER) == 3


# --------------------------------------------------------------------------
# orders from before the move
# --------------------------------------------------------------------------


def test_an_order_with_no_shopify_id_still_cancels_the_old_way(priced, shopify):
    """Three orders predate this work. They have no remote order to cancel, and
    their stock only ever moved locally."""
    from domain.models import Variant

    shopify.set(VARIANT, qty=5)
    carts.add(priced, "whatsapp", WHO, VARIANT, 2)
    order_id = place(priced)["order_id"]

    order = priced.get(Order, order_id)
    order.shopify_order_id = None
    order.shopify_order_name = None
    priced.flush()
    before = priced.get(Variant, VARIANT).stock_qty

    orders.cancel(priced, order)

    assert priced.get(Variant, VARIANT).stock_qty == before + 2
    assert next(iter(shopify.orders.values()))["cancelled"] is False


# --------------------------------------------------------------------------
# the order says who placed it
# --------------------------------------------------------------------------
#
# Without a customer on the order, the admin's Orders list reads "No customer"
# for every sale the bot ever made, and `numberOfOrders` stays empty -- so
# nothing downstream can tell a returning buyer from a first-time one. Shopify
# matches `toUpsert` on the phone, so a returning customer links to the record
# they already have instead of gaining a second one.
#
# These reach for the real `create_order` rather than the fake shelf, which
# replaces the whole function: what is under test is the payload it builds, so
# the transport is what has to be stood in for. Hence `no_shopify`.


class Recorder:
    """Stands in for the GraphQL transport. `replies` are returned in order;
    a callable is invoked with the variables so a test can refuse the first
    call and accept the second."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, query, variables):
        self.calls.append(variables)
        reply = self.replies.pop(0) if self.replies else self.replies
        return reply(variables) if callable(reply) else reply


CREATED = {"orderCreate": {"order": {"id": "gid://shopify/Order/1", "name": "#1029"}}}


def refusal(*errors):
    return {"orderCreate": {"order": None, "userErrors": list(errors)}}


def sell(monkeypatch, recorder, **overrides):
    monkeypatch.setattr(shopify_orders, "get_client", lambda: recorder)
    args = {
        "reference": "WNS-1",
        "items": [{"shopify_variant_id": "gid://shopify/ProductVariant/1",
                   "quantity": 1, "unit_price": 800}],
        "customer_name": "Hazem Abdelhamid",
        "phone": "01067177128",
        "email": None,
        "address": "12 El Sebki Street",
        "governorate": "Cairo",
        "shipping_fee": 60,
    }
    args.update(overrides)
    return shopify_orders.create_order(**args)


@pytest.mark.no_shopify
def test_the_order_carries_the_customer_who_placed_it(monkeypatch):
    recorder = Recorder(CREATED)

    sell(monkeypatch, recorder)

    customer = recorder.calls[0]["order"]["customer"]["toUpsert"]
    assert customer["phone"] == "+201067177128"
    assert customer["firstName"] == "Hazem"
    assert customer["lastName"] == "Abdelhamid"


@pytest.mark.no_shopify
def test_the_customer_and_the_packing_slip_split_the_name_the_same_way(monkeypatch):
    """One helper does the split for both, so the Customers list and the
    address on the parcel can never disagree about the same person."""
    recorder = Recorder(CREATED)

    sell(monkeypatch, recorder, customer_name="حازم عبد الحميد")

    order = recorder.calls[0]["order"]
    customer = order["customer"]["toUpsert"]
    assert (customer["firstName"], customer["lastName"]) == (
        order["shippingAddress"]["firstName"],
        order["shippingAddress"]["lastName"],
    )


@pytest.mark.no_shopify
def test_a_customer_with_nothing_to_match_on_is_left_off(monkeypatch):
    """Shopify cannot look a name up and will not create a customer from one.
    Sending the block anyway risks the whole order, and a rejected order loses
    the sale -- the same trade `_address` makes with an unreadable phone."""
    recorder = Recorder(CREATED)

    sell(monkeypatch, recorder, phone="not a number", email=None)

    assert "customer" not in recorder.calls[0]["order"]


@pytest.mark.no_shopify
def test_an_email_alone_is_enough_to_attach_them(monkeypatch):
    recorder = Recorder(CREATED)

    sell(monkeypatch, recorder, phone="", email="hazem@example.com")

    assert recorder.calls[0]["order"]["customer"]["toUpsert"]["email"] == "hazem@example.com"


@pytest.mark.no_shopify
def test_a_refused_customer_costs_the_link_not_the_sale(monkeypatch):
    """Their phone sitting on somebody else's record is a reason to file the
    order without a customer, not a reason to lose the order."""
    recorder = Recorder(
        refusal({"field": ["order", "customer", "toUpsert", "phone"],
                 "message": "Phone has already been taken"}),
        CREATED,
    )

    result = sell(monkeypatch, recorder)

    assert result["name"] == "#1029"
    assert len(recorder.calls) == 2
    assert "customer" not in recorder.calls[1]["order"]
    # The retry is the same sale, not a re-quoted one.
    assert recorder.calls[1]["order"]["lineItems"] == recorder.calls[0]["order"]["lineItems"]


@pytest.mark.no_shopify
def test_an_empty_shelf_is_not_retried(monkeypatch):
    """The second call is refused for the same reason, and the customer is owed
    the alternatives this refusal raises for."""
    recorder = Recorder(
        refusal({"field": ["order", "lineItems"],
                 "message": "Insufficient inventory for the requested quantity"}),
        CREATED,
    )

    with pytest.raises(shopify_orders.OrderRejected) as caught:
        sell(monkeypatch, recorder)

    assert caught.value.is_out_of_stock
    assert len(recorder.calls) == 1


@pytest.mark.no_shopify
def test_a_refusal_that_is_not_about_the_customer_still_propagates(monkeypatch):
    """The link is dropped on any refusal but an empty shelf, because which
    `field` path Shopify reports a phone conflict in is not something to bet a
    sale on. A refusal that was about something else is refused again the same
    way -- and that is what the caller sees. No order is quietly created."""
    price = {"field": ["order", "shippingLines"], "message": "Price is invalid"}
    recorder = Recorder(refusal(price), refusal(price))

    with pytest.raises(shopify_orders.OrderRejected) as caught:
        sell(monkeypatch, recorder)

    assert "Price is invalid" in str(caught.value)
    assert len(recorder.calls) == 2
    assert "customer" not in recorder.calls[1]["order"]


# --------------------------------------------------------------------------
# the channel tag
# --------------------------------------------------------------------------
#
# The admin's Channel column says "Chatbot Integration" for every order the bot
# places, which is the shop owner's own rule for reading it: the app means the
# bot, and then the tag says which conversation. The tag was the literal string
# `whatsapp` on every order the bot ever placed, Instagram sales included --
# so the admin quietly disagreed with the dashboard about where a sale came
# from, and the disagreement was invisible because both said "whatsapp".


@pytest.mark.no_shopify
def test_an_instagram_sale_is_tagged_instagram(monkeypatch):
    recorder = Recorder(CREATED)

    sell(monkeypatch, recorder, channel="instagram_dm")

    tags = recorder.calls[0]["order"]["tags"]
    assert "instagram" in tags
    assert "whatsapp" not in tags
    # `chatbot` is what staff filter the admin by; it is on every bot order
    # whichever conversation placed it.
    assert "chatbot" in tags


@pytest.mark.no_shopify
def test_a_whatsapp_sale_is_tagged_whatsapp(monkeypatch):
    recorder = Recorder(CREATED)

    sell(monkeypatch, recorder, channel="whatsapp")

    assert "whatsapp" in recorder.calls[0]["order"]["tags"]


@pytest.mark.no_shopify
def test_a_sale_from_a_channel_nobody_named_still_carries_a_channel_tag(monkeypatch):
    """An untagged bot order reads as a website order in the admin, which is
    the one thing the tag exists to prevent. WhatsApp is the bot's own default
    channel, and a wrong-but-present tag is recoverable where a missing one is
    not."""
    recorder = Recorder(CREATED)

    sell(monkeypatch, recorder)

    assert "whatsapp" in recorder.calls[0]["order"]["tags"]
