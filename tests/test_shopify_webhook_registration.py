"""Registering the order-status webhook subscriptions
`backend/webhooks/shopify.py` needs Shopify to push -- see
`backend/services/shopify_webhooks.py`'s module docstring for why this is
only half of "the webhook works" (the signing secret is a manual step).

Not to be confused with `tests/test_shopify_webhooks.py`, which covers the
receiving side (`backend/webhooks/shopify.py` itself: signature, idempotency,
status transitions) -- this file is only about getting Shopify to subscribe
in the first place.
"""

from __future__ import annotations

from integrations.shopify import webhook_registration as shopify_webhooks

URL = "https://wanas-production.up.railway.app/webhooks/shopify"


def test_all_four_topics_are_created_on_a_fresh_store(shopify):
    report = shopify_webhooks.register_missing(URL)

    assert sorted(report["created"]) == sorted(shopify_webhooks.TOPICS)
    assert report["already_present"] == []
    assert report["problems"] == []
    registered_topics = {n["topic"] for n in shopify.webhook_subscriptions}
    assert registered_topics == set(shopify_webhooks.TOPICS.values())


def test_running_it_twice_does_not_create_duplicates(shopify):
    shopify_webhooks.register_missing(URL)
    second = shopify_webhooks.register_missing(URL)

    assert second["created"] == []
    assert sorted(second["already_present"]) == sorted(shopify_webhooks.TOPICS)
    assert len(shopify.webhook_subscriptions) == len(shopify_webhooks.TOPICS)


def test_only_the_missing_topics_are_created(shopify):
    shopify.create_subscription("ORDERS_FULFILLED", URL)

    report = shopify_webhooks.register_missing(URL)

    assert "orders/fulfilled" not in report["created"]
    assert "orders/fulfilled" in report["already_present"]
    assert "orders/cancelled" in report["created"]


def test_a_subscription_for_a_different_url_is_not_treated_as_present(shopify):
    shopify.create_subscription("ORDERS_FULFILLED", "https://a-staging-env.example/webhooks/shopify")

    report = shopify_webhooks.register_missing(URL)

    assert "orders/fulfilled" in report["created"]


def test_a_rejected_topic_is_reported_not_raised(shopify):
    shopify.rejected_webhook_topics.add("ORDERS_CANCELLED")

    report = shopify_webhooks.register_missing(URL)

    assert "orders/cancelled" not in report["created"]
    assert any("orders/cancelled" in p for p in report["problems"])
    # The other three still went through -- one rejection does not block the rest.
    assert len(report["created"]) == 3


def test_a_total_outage_propagates_for_the_caller_to_handle(shopify):
    """`register_missing` does not defend against Shopify being entirely
    unreachable on its own -- the caller (app startup) does, the same way
    it already does for every other optional Shopify integration run at
    boot (see app.py's `_import_missing_shopify_products` for the pattern
    this follows)."""
    import pytest

    from integrations.shopify.catalog import ShopifyUnavailable

    shopify.down = True
    with pytest.raises(ShopifyUnavailable):
        shopify_webhooks.register_missing(URL)
