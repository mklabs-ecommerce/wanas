"""The customer block on `orderCreate`, and Shopify's rule about its keys.

This file exists because the whole suite was green while production could not
place a single order. `create_order` is replaced wholesale by the fake shelf
(tests/fake_shopify.py), so nothing ever built the real GraphQL payload and
nothing ever saw that Shopify refuses it:

    OrderCreateUpsertCustomerAttributesInput requires at least one of id, email

A phone is not a key `toUpsert` can match or create on. This shop is cash on
delivery and never asks for an email, so that refusal was every order, not an
edge case -- the bot took the name, the address and the phone, asked
"تأكيد الأوردر؟", and then handed the customer to a person because
`confirm_order` came back an error.

So these tests go under the fake, at the payload itself.
"""

from __future__ import annotations

import pytest

from integrations.shopify import orders as shopify_orders

NAME = "حازم عبدالحميد"
PHONE = "+201067177129"
GID = "gid://shopify/Customer/551"


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------


def test_a_phone_alone_is_not_a_customer_key():
    """The bug. A name and a phone is what every order here has, and sending
    it as `toUpsert` made Shopify refuse the entire order."""
    assert shopify_orders._customer(name=NAME, phone=PHONE, email=None) is None


def test_no_identifier_at_all_is_still_nothing():
    assert shopify_orders._customer(name=NAME, phone=None, email=None) is None


def test_an_email_is_a_key_shopify_accepts():
    block = shopify_orders._customer(name=NAME, phone=PHONE, email="h@example.com")
    assert block["toUpsert"]["email"] == "h@example.com"
    # The phone still rides along; it is just not what the record is keyed on.
    assert block["toUpsert"]["phone"] == PHONE


def test_a_resolved_customer_id_is_a_key_shopify_accepts():
    block = shopify_orders._customer(name=NAME, phone=PHONE, email=None, customer_gid=GID)
    assert block["toUpsert"]["id"] == GID
    assert block["toUpsert"]["phone"] == PHONE
    assert block["toUpsert"]["firstName"] == "حازم"


@pytest.mark.parametrize(
    "block",
    [
        shopify_orders._customer(name=NAME, phone=PHONE, email=None),
        shopify_orders._customer(name=NAME, phone=None, email=None),
        shopify_orders._customer(name=NAME, phone=PHONE, email="h@example.com"),
        shopify_orders._customer(name=NAME, phone=PHONE, email=None, customer_gid=GID),
    ],
)
def test_every_customer_block_we_ever_build_carries_a_key_shopify_accepts(block):
    """The invariant, stated once over every shape the builder can return:
    either there is no block, or it has an id or an email."""
    if block is None:
        return
    attributes = block["toUpsert"]
    assert attributes.get("id") or attributes.get("email"), attributes


# --------------------------------------------------------------------------
# Resolving an id from the phone
# --------------------------------------------------------------------------


def test_a_returning_customer_is_found_by_phone_and_keyed_by_id(monkeypatch):
    from integrations.shopify import admin_customers

    monkeypatch.setattr(
        admin_customers, "find_customer", lambda **kw: {"id": GID, "phone": kw.get("phone")}
    )
    assert shopify_orders._find_customer_gid(PHONE) == GID


def test_an_unknown_phone_resolves_to_nothing_rather_than_failing(monkeypatch):
    from integrations.shopify import admin_customers

    monkeypatch.setattr(admin_customers, "find_customer", lambda **kw: None)
    assert shopify_orders._find_customer_gid(PHONE) is None


def test_a_lookup_that_raises_never_costs_the_sale(monkeypatch):
    """Nothing this lookup can tell us is worth losing an order over."""
    from integrations.shopify import admin_customers

    def boom(**_kw):
        raise RuntimeError("shopify is having a day")

    monkeypatch.setattr(admin_customers, "find_customer", boom)
    assert shopify_orders._find_customer_gid(PHONE) is None


def test_no_phone_means_no_lookup(monkeypatch):
    from integrations.shopify import admin_customers

    def boom(**_kw):  # pragma: no cover - must not be reached
        raise AssertionError("looked a customer up with no phone")

    monkeypatch.setattr(admin_customers, "find_customer", boom)
    assert shopify_orders._find_customer_gid(None) is None
    assert shopify_orders._find_customer_gid("") is None
