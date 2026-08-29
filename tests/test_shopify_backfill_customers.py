"""Attaching a customer to orders that were placed without one.

The repair script writes to Shopify, so what matters is what it *decides*
before it writes: which orders it touches, who it thinks placed them, and --
most of all -- which orders it leaves alone. A backfill that links the wrong
person is worse than the "No customer" it replaces.
"""

from __future__ import annotations

import pytest

from scripts import shopify_backfill_customers as backfill


def order(**overrides):
    node = {
        "id": "gid://shopify/Order/1",
        "name": "#1028",
        "createdAt": "2026-08-26T10:00:00Z",
        "email": None,
        "customer": None,
        "shippingAddress": {
            "name": "حازم عبد الحميد",
            "firstName": "حازم",
            "lastName": "عبد الحميد",
            "phone": "01067177128",
        },
    }
    node.update(overrides)
    return node


# --------------------------------------------------------------------------
# which orders it touches
# --------------------------------------------------------------------------


def test_an_order_that_already_has_a_customer_is_left_alone():
    """Idempotence is the whole safety property here -- a re-run after a
    partial failure must touch only what is left."""
    assert backfill.plan_for(order(customer={"id": "gid://shopify/Customer/5"})) is None


def test_an_order_with_no_way_to_identify_the_buyer_is_skipped():
    """A name is not an identity. Two customers who share one are one
    customer to this script, and linking them would be worse than the
    "No customer" it is replacing."""
    plan = backfill.plan_for(
        order(shippingAddress={"name": "حازم", "firstName": "حازم", "lastName": "", "phone": None})
    )

    assert plan is not None and "skip" in plan


def test_an_email_alone_is_enough_to_identify_them():
    plan = backfill.plan_for(
        order(email="hazem@example.com",
              shippingAddress={"name": "Hazem", "firstName": "Hazem", "lastName": "", "phone": None})
    )

    assert "skip" not in plan
    assert plan["email"] == "hazem@example.com"
    assert plan["phone"] is None


# --------------------------------------------------------------------------
# who it thinks placed them
# --------------------------------------------------------------------------


def test_the_phone_is_normalised_the_way_the_order_path_does_it():
    """The same `normalise_phone`, so a backfilled customer is found by the
    same search a live order's `toUpsert` would use. A record created under
    `01067177128` would be a second copy of a person Shopify already has
    under `+201067177128`."""
    assert backfill.plan_for(order())["phone"] == "+201067177128"


def test_a_name_shopify_already_split_is_taken_as_split():
    plan = backfill.plan_for(order())

    assert (plan["first"], plan["last"]) == ("حازم", "عبد الحميد")


def test_a_combined_name_is_split_the_same_way_the_bot_splits_it():
    """`_split_name` is imported, not reimplemented, so a backfilled record
    matches the shape of one created live."""
    first, last = backfill.names_from({"name": "حازم عبد الحميد"})

    assert (first, last) == ("حازم", "عبد الحميد")


def test_an_address_with_no_name_at_all_still_yields_an_identity():
    """Nameless but reachable: the phone is what links the order, and a
    customer record with a phone and no name is still better than none."""
    plan = backfill.plan_for(
        order(shippingAddress={"name": "", "firstName": "", "lastName": "", "phone": "01067177128"})
    )

    assert "skip" not in plan
    assert plan["first"] == "" and plan["last"] == ""


# --------------------------------------------------------------------------
# the dry run tells the truth
# --------------------------------------------------------------------------


@pytest.fixture()
def shop(monkeypatch):
    """A fake Shopify that records writes instead of making them."""

    class Shop:
        def __init__(self):
            self.orders: list[dict] = []
            self.customers: list[dict] = []
            self.links: list[tuple[str, str]] = []

        def iter_orders(self, query, max_pages=20):
            return iter(self.orders)

        def find_customer(self, *, phone=None, email=None):
            for customer in self.customers:
                if phone and customer.get("phone") == phone:
                    return customer
                if email and customer.get("email") == email:
                    return customer
            return None

        def create_customer(self, *, first_name, last_name, phone, email):
            customer = {
                "id": f"gid://shopify/Customer/{len(self.customers) + 1}",
                "displayName": f"{first_name} {last_name}".strip(),
                "phone": phone,
                "email": email,
            }
            self.customers.append(customer)
            return customer

        def set_order_customer(self, order_gid, customer_gid):
            self.links.append((order_gid, customer_gid))
            return {"id": order_gid}

    shop = Shop()
    monkeypatch.setattr(backfill, "iter_orders", shop.iter_orders)
    monkeypatch.setattr(backfill.admin_customers, "find_customer", shop.find_customer)
    monkeypatch.setattr(backfill.admin_customers, "create_customer", shop.create_customer)
    monkeypatch.setattr(backfill.admin_customers, "set_order_customer", shop.set_order_customer)
    return shop


def two_orders_from_one_person():
    return [
        order(id="gid://shopify/Order/1", name="#1001"),
        order(id="gid://shopify/Order/2", name="#1002"),
    ]


def test_a_dry_run_writes_nothing(shop, capsys):
    shop.orders = two_orders_from_one_person()

    backfill.run(apply=False, tag=None, max_pages=20)

    assert shop.customers == [] and shop.links == []
    assert "nothing was written" in capsys.readouterr().out


def test_the_dry_run_does_not_promise_more_customers_than_it_will_create(shop, capsys):
    """Two orders from one person are one customer. Counting per order would
    tell the operator it is about to create twice what it will, which is the
    kind of number that stops a person applying a correct repair."""
    shop.orders = two_orders_from_one_person()

    backfill.run(apply=False, tag=None, max_pages=20)

    assert "would link 2 (1 new customer)" in capsys.readouterr().out


def test_applying_creates_one_customer_and_links_both_orders(shop):
    shop.orders = two_orders_from_one_person()

    backfill.run(apply=True, tag=None, max_pages=20)

    assert len(shop.customers) == 1
    assert [gid for gid, _ in shop.links] == [
        "gid://shopify/Order/1",
        "gid://shopify/Order/2",
    ]
    assert len({customer for _, customer in shop.links}) == 1


def test_an_existing_customer_is_linked_rather_than_duplicated(shop):
    shop.customers.append(
        {"id": "gid://shopify/Customer/99", "displayName": "Already Here",
         "phone": "+201067177128", "email": None}
    )
    shop.orders = [order()]

    backfill.run(apply=True, tag=None, max_pages=20)

    assert len(shop.customers) == 1
    assert shop.links == [("gid://shopify/Order/1", "gid://shopify/Customer/99")]


def test_one_order_failing_does_not_stop_the_rest(shop, capsys, monkeypatch):
    """A refusal on one order -- a phone another record owns, say -- is that
    order's problem. Stopping would leave the repair half-done with no way to
    tell which half."""
    from integrations.shopify.admin_customers import CustomerWriteRefused

    calls = {"n": 0}
    real_create = shop.create_customer

    def sometimes_refuses(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CustomerWriteRefused("customerCreate: Phone has already been taken")
        return real_create(**kwargs)

    monkeypatch.setattr(backfill.admin_customers, "create_customer", sometimes_refuses)
    shop.orders = [
        order(id="gid://shopify/Order/1", name="#1001"),
        order(id="gid://shopify/Order/2", name="#1002",
              shippingAddress={"name": "Other Person", "firstName": "Other",
                               "lastName": "Person", "phone": "01099988877"}),
    ]

    exit_code = backfill.run(apply=True, tag=None, max_pages=20)

    assert len(shop.links) == 1
    assert exit_code == 1, "a failure has to be visible in the exit code"
    assert "1 failed" in capsys.readouterr().out


def test_a_second_order_does_not_wait_for_shopifys_search_index(shop, monkeypatch):
    """Shopify's customer search is an index and the index lags the write: a
    customer created a second ago is not findable yet. On the first real run
    six of nineteen orders failed exactly this way -- the next order from the
    same person searched, found nothing, tried to create a duplicate, and was
    correctly refused with "Phone has already been taken". What this run
    created, this run has to remember."""
    from integrations.shopify.admin_customers import CustomerWriteRefused

    monkeypatch.setattr(
        backfill.admin_customers, "find_customer",
        lambda *, phone=None, email=None: None,  # the index never catches up
    )

    real_create = shop.create_customer

    def refuses_a_duplicate(*, first_name, last_name, phone, email):
        if any(c.get("phone") == phone for c in shop.customers):
            raise CustomerWriteRefused("customerCreate: Phone has already been taken")
        return real_create(first_name=first_name, last_name=last_name, phone=phone, email=email)

    monkeypatch.setattr(backfill.admin_customers, "create_customer", refuses_a_duplicate)
    shop.orders = two_orders_from_one_person()

    exit_code = backfill.run(apply=True, tag=None, max_pages=20)

    assert exit_code == 0, "the second order must not fail"
    assert len(shop.customers) == 1
    assert len(shop.links) == 2
