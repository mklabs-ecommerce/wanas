"""Taking stock off the Shopify shelf when the bot sells something.

The storefront decrements Shopify by itself. WhatsApp orders did not, which is
what left the two sides able to sell the same last item twice. This module is
the missing half.

What makes this safe rather than merely convenient is `compareQuantity`: every
write says both what the new total should be *and* what we believe the current
one is, and Shopify refuses the whole call if the shelf moved since we looked.
That turns "two customers raced for the last shirt" from a silent oversell into
an error we can answer honestly.

That is why this uses `inventorySetQuantities` rather than the delta-based
`inventoryAdjustQuantities`. A delta is atomic, which sounds like the safer
choice, but atomic is not the same as correct here: `-1` applied to a shelf
that dropped to zero while we were reading it succeeds and leaves the count at
-1. Only the compare rejects that, and the compare lives on set, not adjust.
"""

from __future__ import annotations

import logging
import threading

from backend.integrations.shopify_client import (
    ShopifyConfigError,
    ShopifyUnavailable,
    get_client,
)

log = logging.getLogger("wanas.shopify.inventory")


class StockMoved(RuntimeError):
    """Shopify refused the adjustment because the quantity changed under us.

    Distinct from ShopifyUnavailable: the store is fine, the shelf is not what
    we thought. The customer needs a different answer for each.
    """


LOCATIONS_QUERY = """
{ locations(first: 5) { nodes { id name isActive } } }
"""

SET_MUTATION = """
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message code }
  }
}
"""

_location_id: str | None = None
_location_lock = threading.Lock()


def location_id() -> str:
    """The one location the shop stocks from, looked up once.

    Cached for the life of the process: a shop does not grow a second warehouse
    mid-conversation, and paying a round trip for it on every order would
    double the latency of the slowest moment in the whole flow.
    """
    global _location_id
    with _location_lock:
        if _location_id:
            return _location_id

    nodes = (get_client()(LOCATIONS_QUERY).get("locations") or {}).get("nodes") or []
    active = [n for n in nodes if n.get("isActive")] or nodes
    if not active:
        raise ShopifyUnavailable("the store has no inventory location")

    with _location_lock:
        _location_id = active[0]["id"]
    log.info("Shopify inventory location: %s (%s)", active[0].get("name"), _location_id)
    return _location_id


def reset_location() -> None:
    """Tests and the verification script, after changing the environment."""
    global _location_id
    with _location_lock:
        _location_id = None


def _set(changes: list[dict], reason: str, note: str) -> None:
    """`changes` is [{inventory_item_id, was, now}].

    Raises StockMoved if Shopify rejected any line, ShopifyUnavailable if the
    call itself failed. Never returns a partial success quietly.
    """
    if not changes:
        return

    payload = {
        "input": {
            "reason": reason,
            "name": "available",
            "referenceDocumentUri": note,
            # The default is False, but stating it is worth the line: flipping
            # it to True is exactly how this becomes a last-write-wins update
            # that can oversell, and `shopify_sync.py` sets it True two files
            # away for its bulk corrections.
            "ignoreCompareQuantity": False,
            "quantities": [
                {
                    "inventoryItemId": change["inventory_item_id"],
                    "locationId": location_id(),
                    "quantity": change["now"],
                    # Shopify checks this against the current value and refuses
                    # the whole call if it does not match.
                    "compareQuantity": change["was"],
                }
                for change in changes
            ],
        }
    }

    data = get_client()(SET_MUTATION, payload)
    result = data.get("inventorySetQuantities") or {}
    errors = result.get("userErrors") or []
    if not errors:
        return

    messages = "; ".join(e.get("message", "") for e in errors)
    log.warning("Shopify refused an inventory write: %s", messages)

    if not _is_compare_mismatch(errors):
        # Everything that is not "the shelf moved" is our problem: a bad
        # reason, a location the token cannot touch, a schema change. Calling
        # those StockMoved would walk back up to the customer as "sold out" --
        # a lie about an item that is sitting on the shelf, and one that sends
        # them to another shop. Fail as an outage instead, which refuses the
        # order and hands the conversation to a person.
        raise ShopifyUnavailable(messages)

    raise StockMoved(messages)


#: Substrings that identify the one failure that really is a lost race. Matched
#: on the code first; the message is a fallback because Shopify has renamed
#: these codes before.
_COMPARE_CODES = {"STALE_INVENTORY_QUANTITY", "INVALID_QUANTITY_COMPARE"}


def _is_compare_mismatch(errors: list[dict]) -> bool:
    for error in errors:
        if (error.get("code") or "").upper() in _COMPARE_CODES:
            return True
        message = (error.get("message") or "").lower()
        if "compare" in message and ("quantity" in message or "match" in message):
            return True
        if "stale" in message:
            return True
    return False


def reserve(changes: list[dict], order_ref: str) -> None:
    """Take items off the shelf for an order.

    `changes` carries positive quantities; the direction is applied here so no
    caller can accidentally add stock by passing the wrong number.
    """
    _set(
        [
            {
                "inventory_item_id": c["inventory_item_id"],
                "was": int(c["expected"]),
                "now": int(c["expected"]) - abs(int(c["quantity"])),
            }
            for c in changes
        ],
        reason="reservation_created",
        note=f"wanas-bot://order/{order_ref}",
    )


def release(changes: list[dict], order_ref: str) -> None:
    """Put items back -- a cancellation, a reduced quantity, or a local write
    that failed after the reservation went through.

    Deliberately best-effort in its *callers*, never here: this raises like
    anything else, and the caller decides whether a failed release is worth
    failing the customer over. It usually is not -- the stock is recoverable by
    hand, the customer's cancellation is not.
    """
    _set(
        [
            {
                "inventory_item_id": c["inventory_item_id"],
                "was": int(c["expected"]),
                "now": int(c["expected"]) + abs(int(c["quantity"])),
            }
            for c in changes
        ],
        reason="reservation_deleted",
        note=f"wanas-bot://release/{order_ref}",
    )


def try_release(changes: list[dict], order_ref: str) -> bool:
    """`release` that swallows its failures and says so in the log.

    Used on the compensating path, where raising would replace one problem
    (stock held for an order that does not exist) with a worse one (an
    exception surfacing to a customer who has already been told nothing was
    charged).
    """
    try:
        release(changes, order_ref)
        return True
    except (ShopifyUnavailable, ShopifyConfigError, StockMoved) as exc:
        log.error(
            "Could not return stock to Shopify for %s -- fix by hand: %s | %s",
            order_ref,
            exc,
            changes,
        )
        return False
