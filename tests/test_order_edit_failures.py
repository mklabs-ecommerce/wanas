"""Why an order edit did not happen, said in a way a person can act on.

Approving an add on production returned `store_unavailable`. The real Shopify
response was:

    Access denied for orderEditBegin field.
    Required access: Requires `write_order_edits` access scope.

The Admin token was missing a scope, so *every* order edit -- an add approval,
a swap approval, a quantity change -- could never have succeeded. Staff read
"store unavailable", which names nothing and suggests waiting, and pressed the
button three times.

Two separate defects, pinned separately here. The **reading**: every failure
that was not out-of-stock collapsed into one word, so a permanent
configuration fault and a thirty-second outage were indistinguishable. And the
**atomicity**, which held but was never tested: a Shopify edit that does not
commit must leave the local order exactly as it was.
"""

from __future__ import annotations

import pytest

from domain.models import Order
from domain.services import carts, orders
from integrations.shopify import catalog as shopify_catalog, orders as shopify_orders

VARIANT_A = "wanas-hoodie-s-olive"
VARIANT_B = "wanas-hoodie-s-black"
CUSTOMER = "201555000333"

#: The production error, verbatim from the Shopify response.
ACCESS_DENIED = [
    {
        "message": (
            "Access denied for orderEditBegin field. Required access: "
            "Requires `write_order_edits` access scope."
        ),
        "extensions": {
            "code": "ACCESS_DENIED",
            "requiredAccess": "Requires `write_order_edits` access scope.",
        },
        "path": ["orderEditBegin"],
    }
]


@pytest.fixture()
def order(cairo_rate, seeded) -> Order:
    carts.add(seeded, "whatsapp", CUSTOMER, VARIANT_A, 1)
    result = orders.place_order(
        seeded,
        channel="whatsapp",
        external_id=CUSTOMER,
        customer_name="Hazem",
        governorate="Cairo",
        address="1 Test Street",
        contact_phone="01055566677",
    )
    assert "error" not in result, result
    seeded.flush()
    return seeded.get(Order, result["order_id"])


# --------------------------------------------------------------------------
# reading the failure
# --------------------------------------------------------------------------


def test_the_production_error_is_recognised_as_a_missing_scope():
    """Read off `extensions`, not pattern-matched on the message text."""
    from integrations.shopify.client import _denied_scope

    assert _denied_scope(ACCESS_DENIED) == "write_order_edits"


@pytest.mark.parametrize(
    "errors",
    [
        [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
        [{"message": "boom"}],
        [],
    ],
)
def test_an_ordinary_failure_is_not_mistaken_for_a_permission_one(errors):
    from integrations.shopify.client import _denied_scope

    assert _denied_scope(errors) is None


def test_an_access_denial_with_no_named_scope_is_still_a_denial():
    """Falsy is not the signal -- `None` is. "Denied but unnamed" must not
    read as a transient outage, because retrying it never works either."""
    from integrations.shopify.client import _denied_scope

    assert _denied_scope([{"message": "no", "extensions": {"code": "ACCESS_DENIED"}}]) == ""


@pytest.mark.parametrize("apply", ["add", "swap", "quantity"])
def test_a_missing_scope_reaches_staff_as_a_permission_problem(order, seeded, monkeypatch, apply):
    """All three edit paths, because the scope is missing for all three and
    only one of them was ever pressed."""

    def denied(*args, **kwargs):
        raise shopify_catalog.ShopifyAccessDenied("denied", "write_order_edits")

    monkeypatch.setattr(shopify_orders, "add_line", denied)
    monkeypatch.setattr(shopify_orders, "swap_line", denied)
    monkeypatch.setattr(shopify_orders, "set_line_quantity", denied)

    if apply == "add":
        result = orders.apply_add(seeded, order, VARIANT_B, 1)
    elif apply == "swap":
        result = orders.apply_swap(seeded, order, VARIANT_A, VARIANT_B)
    else:
        result = orders.modify_quantity(seeded, order, VARIANT_A, 2)

    assert result["error"] == "store_permission", result
    # The scope travels with it, so nobody has to go and read a log to learn
    # which permission to grant.
    assert result["scope"] == "write_order_edits"


def test_shopify_refusing_the_edit_is_told_apart_from_being_unreachable(order, seeded, monkeypatch):
    def refused(*args, **kwargs):
        raise shopify_orders.OrderRejected(
            "The order has been fulfilled and cannot be edited",
            [{"message": "The order has been fulfilled and cannot be edited"}],
        )

    monkeypatch.setattr(shopify_orders, "add_line", refused)
    result = orders.apply_add(seeded, order, VARIANT_B, 1)
    assert result["error"] == "store_refused"
    assert "fulfilled" in result["detail"]


def test_an_unreachable_store_still_says_unavailable(order, seeded, monkeypatch):
    """The word keeps its meaning now that it is no longer the answer to
    everything: this one really is worth retrying."""

    def down(*args, **kwargs):
        raise shopify_catalog.ShopifyUnavailable("connection reset")

    monkeypatch.setattr(shopify_orders, "add_line", down)
    assert orders.apply_add(seeded, order, VARIANT_B, 1)["error"] == "store_unavailable"


def test_out_of_stock_still_wins_over_every_other_reading(order, seeded, monkeypatch):
    def gone(*args, **kwargs):
        raise shopify_orders.OrderRejected(
            "Not enough inventory available",
            [{"message": "Not enough inventory available"}],
        )

    monkeypatch.setattr(shopify_orders, "add_line", gone)
    result = orders.apply_add(seeded, order, VARIANT_B, 1)
    assert result["error"] == "insufficient_stock"


# --------------------------------------------------------------------------
# and a failed edit changes nothing at all
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boom",
    [
        lambda: shopify_catalog.ShopifyAccessDenied("denied", "write_order_edits"),
        lambda: shopify_orders.OrderRejected("fulfilled already"),
        lambda: shopify_catalog.ShopifyUnavailable("down"),
    ],
)
def test_a_failed_add_leaves_the_order_exactly_as_it_was(order, seeded, monkeypatch, boom):
    """However it fails. A half-applied edit is a customer billed at the door
    for a line Shopify never received."""
    before_lines = {i.variant_id: i.quantity for i in order.items}
    before_total = order.total

    def fail(*args, **kwargs):
        raise boom()

    monkeypatch.setattr(shopify_orders, "add_line", fail)
    assert "error" in orders.apply_add(seeded, order, VARIANT_B, 1)

    seeded.flush()
    after = seeded.get(Order, order.order_id)
    assert {i.variant_id: i.quantity for i in after.items} == before_lines
    assert after.total == before_total


def test_a_failed_swap_leaves_the_order_exactly_as_it_was(order, seeded, monkeypatch):
    before_lines = {i.variant_id: i.quantity for i in order.items}
    before_total = order.total

    def fail(*args, **kwargs):
        raise shopify_catalog.ShopifyAccessDenied("denied", "write_order_edits")

    monkeypatch.setattr(shopify_orders, "swap_line", fail)
    assert orders.apply_swap(seeded, order, VARIANT_A, VARIANT_B)["error"] == "store_permission"

    seeded.flush()
    after = seeded.get(Order, order.order_id)
    # The original line is still on the order -- the whole point of a swap
    # failing cleanly rather than halfway.
    assert {i.variant_id: i.quantity for i in after.items} == before_lines
    assert after.total == before_total


# --------------------------------------------------------------------------
# ...and it is visible before anyone presses a button.
# --------------------------------------------------------------------------


def test_the_missing_scope_is_reported_at_boot(monkeypatch, caplog):
    """`shopify_configured` was true the whole time. The question worth
    asking at boot is not whether a token is set."""
    from integrations.shopify import scopes

    monkeypatch.setattr(
        scopes, "granted", lambda: set(scopes.REQUIRED_SCOPES) - {"write_order_edits"}
    )
    with caplog.at_level("ERROR", logger="wanas.shopify.scopes"):
        gaps = scopes.log_scope_check()
    assert gaps == ["write_order_edits"]
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "write_order_edits" in message
    assert "reinstall" in message


def test_a_complete_token_reports_nothing(monkeypatch):
    from integrations.shopify import scopes

    monkeypatch.setattr(scopes, "granted", lambda: set(scopes.REQUIRED_SCOPES) | {"read_themes"})
    assert scopes.log_scope_check() == []


def test_an_unreadable_scope_list_is_not_reported_as_a_fault(monkeypatch):
    """"We could not ask" must not render as "the shop has no permissions"."""
    from integrations.shopify import scopes

    monkeypatch.setattr(scopes, "granted", lambda: None)
    assert scopes.missing(refresh=True) == []


def test_a_failure_is_not_cached_as_success(monkeypatch):
    """A single timeout must not silence the check for an hour."""
    from integrations.shopify import scopes

    calls = []

    def flaky():
        calls.append(1)
        return None if len(calls) == 1 else set(scopes.REQUIRED_SCOPES) - {"write_orders"}

    monkeypatch.setattr(scopes, "granted", flaky)
    monkeypatch.setattr(scopes, "_cached", None)
    assert scopes.missing(refresh=True) == []
    assert scopes.missing() == ["write_orders"]


# --------------------------------------------------------------------------
# ...and the edit that worked, and still cost the customer 81.20.
#
# The first add to land on production did everything right except the one
# thing nobody was checking. `orderCreate` is sent no `taxLines`, so every
# order this shop creates carries none -- but `orderEditAddVariant` runs
# Shopify's own tax engine, which put GST 14% (81.20) on a 580.00 garment
# added to order #1040. The customer had been told 1189.00. Shopify recorded
# 1270.20. Cash on delivery collects Shopify's number.
# --------------------------------------------------------------------------


def _totals(remote: str, tax: str = "0"):
    from decimal import Decimal

    return lambda _id: (Decimal(remote), Decimal(tax))


def test_a_total_shopify_disagrees_with_raises_an_alert(order, seeded, monkeypatch):
    from domain.models import QueueKind
    from domain.services import queues

    monkeypatch.setattr(shopify_orders, "current_total", _totals("1270.20", "81.20"))
    orders.check_total_against_shopify(seeded, order, "an add")

    alerts = [
        item
        for item in queues.open_items(seeded, QueueKind.ALERT.value)
        if item.reason == "order_total_mismatch"
    ]
    assert len(alerts) == 1, "the disagreement was not raised"
    # Both numbers, because the point is that they differ. `common.money`
    # renders these as numbers, not fixed-point strings -- house style, so a
    # reply reads "650" rather than "650.00".
    assert "1270.2" in alerts[0].summary
    assert str(order.total) not in alerts[0].summary.split("told ")[0]
    assert alerts[0].payload["shopify_tax"] == 81.2
    assert alerts[0].payload["shopify_total"] == 1270.2


def test_totals_that_agree_raise_nothing(order, seeded, monkeypatch):
    from domain.models import QueueKind
    from domain.services import queues

    monkeypatch.setattr(shopify_orders, "current_total", _totals(str(order.total)))
    orders.check_total_against_shopify(seeded, order, "an add")
    assert not [
        i
        for i in queues.open_items(seeded, QueueKind.ALERT.value)
        if i.reason == "order_total_mismatch"
    ]


def test_a_rounding_difference_is_not_a_disagreement(order, seeded, monkeypatch):
    """Shopify rounds; a piastre is not an argument at the door."""
    from decimal import Decimal

    from domain.models import QueueKind
    from domain.services import queues

    monkeypatch.setattr(
        shopify_orders, "current_total", _totals(str(Decimal(order.total) + Decimal("0.01")))
    )
    orders.check_total_against_shopify(seeded, order, "an add")
    assert not [
        i
        for i in queues.open_items(seeded, QueueKind.ALERT.value)
        if i.reason == "order_total_mismatch"
    ]


def test_an_unreadable_total_is_not_reported_as_a_mismatch(order, seeded, monkeypatch):
    """"We could not ask" is not "they disagree" -- the same rule the scope
    check follows."""
    from domain.models import QueueKind
    from domain.services import queues

    monkeypatch.setattr(shopify_orders, "current_total", lambda _id: None)
    orders.check_total_against_shopify(seeded, order, "an add")
    assert not [
        i
        for i in queues.open_items(seeded, QueueKind.ALERT.value)
        if i.reason == "order_total_mismatch"
    ]


def test_the_check_runs_on_every_edit_path(order, seeded, monkeypatch):
    """A quantity change and a swap edit the same order the same way."""
    import domain.services.orders as orders_module

    seen = []
    monkeypatch.setattr(
        orders_module, "check_total_against_shopify", lambda s, o, what: seen.append(what)
    )
    orders.apply_add(seeded, order, VARIANT_B, 1)
    orders.modify_quantity(seeded, order, VARIANT_A, 2)
    assert seen == ["an add", "a quantity change"], seen
