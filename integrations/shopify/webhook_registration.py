"""Register the order-status webhook subscriptions `integrations/shopify/webhooks.py`
needs Shopify to push. See that module's docstring for what receives them.

Registering a subscription and being able to *verify* what it delivers are
two separate things: this only creates the subscription (Shopify's own
notification of what happened), never `SHOPIFY_WEBHOOK_SECRET` -- that is
the signing secret for this app's API credentials, found in Shopify Admin
under the app's own API credentials page, and only ever visible there. This
module closes the "nothing is even subscribed" half of the gap
automatically; the secret is a one-time manual step documented in
docs/OPERATIONS.md.

Idempotent: `register_missing` checks existing subscriptions for this exact
callback URL before creating anything, so running it on every boot (once
both Shopify and `PUBLIC_BASE_URL` / `RAILWAY_PUBLIC_DOMAIN` are configured)
is safe -- on an already-registered store it does nothing.

The network-calling primitives (`list_subscriptions` / `create_subscription`)
are factored out from the orchestration (`register_missing`) the same way
`shopify_admin_products.py` splits `shopify_create_product` etc. from
`create_product`: only the primitives are what a test's fake replaces, so
the "which topics are missing, don't re-create what's there" logic is real
code every test exercises, not reimplemented by the fake.
"""

from __future__ import annotations

import logging

from integrations.shopify.client import get_admin_client

log = logging.getLogger("wanas.shopify.webhooks")

LIST_QUERY = """
query($cursor: String) {
  webhookSubscriptions(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id topic callbackUrl }
  }
}
"""

CREATE_MUTATION = """
mutation($topic: WebhookSubscriptionTopic!, $input: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $input) {
    webhookSubscription { id topic callbackUrl }
    userErrors { field message }
  }
}
"""

#: The REST-style topic name `integrations/shopify/webhooks.py` matches on, mapped
#: to Shopify's GraphQL topic enum. Kept as the single place this mapping is
#: written, so the two files cannot silently drift apart.
TOPICS: dict[str, str] = {
    "orders/fulfilled": "ORDERS_FULFILLED",
    "orders/partially_fulfilled": "ORDERS_PARTIALLY_FULFILLED",
    "fulfillments/update": "FULFILLMENTS_UPDATE",
    "orders/cancelled": "ORDERS_CANCELLED",
    #: A product created in Shopify Admin, mirrored into wanas.db as it
    #: happens. `product_import` still runs its catalogue-wide reconcile at
    #: boot -- that is the safety net for a delivery Shopify dropped -- but a
    #: boot is the wrong granularity for "staff added a product this
    #: afternoon", and on a shop that does not redeploy it never comes.
    "products/create": "PRODUCTS_CREATE",
    #: And the update, which is not redundant: Shopify fires `create` the
    #: moment Save is pressed, when a product often still wears nothing but
    #: the placeholder variant -- `is_placeholder_only` skips exactly that,
    #: and correctly, since mirroring it writes a phantom "One Size" row at
    #: 0.00. The sizes and colours arrive a moment later as an *update*, and
    #: without this topic that product waits for a redeploy. A product already
    #: known costs one read and returns.
    "products/update": "PRODUCTS_UPDATE",
}


class WebhookRejected(RuntimeError):
    """Shopify refused the subscription and said why -- a bad url or scope,
    not an outage."""


def list_subscriptions() -> list[dict]:
    """Every registered webhook subscription: `[{"id", "topic", "callbackUrl"}, ...]`."""
    client = get_admin_client()
    out: list[dict] = []
    cursor = None
    while True:
        data = client(LIST_QUERY, {"cursor": cursor})
        block = data.get("webhookSubscriptions") or {}
        out.extend(block.get("nodes") or [])
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return out
        cursor = page.get("endCursor")


def create_subscription(topic: str, callback_url: str) -> None:
    """`topic` is Shopify's GraphQL enum name (e.g. `ORDERS_FULFILLED`), not
    the REST-style string. Raises `WebhookRejected` on a Shopify-side refusal."""
    client = get_admin_client()
    data = client(
        CREATE_MUTATION,
        {"topic": topic, "input": {"callbackUrl": callback_url, "format": "JSON"}},
    )
    result = data.get("webhookSubscriptionCreate") or {}
    errors = result.get("userErrors") or []
    if errors:
        message = "; ".join(e.get("message", "") for e in errors)
        log.warning("Shopify rejected a webhook subscription (%s): %s", topic, message)
        raise WebhookRejected(message)


def register_missing(callback_url: str) -> dict:
    """Create whichever of the four topics aren't already subscribed against
    `callback_url`. Returns `{"created": [...], "already_present": [...],
    "problems": [...]}`, all lists of the REST-style topic names.
    """
    existing = {
        node.get("topic") for node in list_subscriptions() if node.get("callbackUrl") == callback_url
    }

    created: list[str] = []
    already_present: list[str] = []
    problems: list[str] = []

    for rest_topic, enum_topic in TOPICS.items():
        if enum_topic in existing:
            already_present.append(rest_topic)
            continue
        try:
            create_subscription(enum_topic, callback_url)
        except Exception as exc:  # WebhookRejected, ShopifyUnavailable, ShopifyConfigError
            problems.append(f"{rest_topic}: {exc}")
        else:
            created.append(rest_topic)

    return {"created": created, "already_present": already_present, "problems": problems}
