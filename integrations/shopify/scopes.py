"""Which Shopify permissions this deployment actually has.

The same shape as `integrations/mail/client.py::check_transport`, and for the
same reason. `SHOPIFY_ADMIN_TOKEN` being *set* is not the question anyone
needs answered -- `/health` said `shopify_configured: true` while every order
edit in production was impossible, because the token was missing
`write_order_edits`. A swap approval, an add approval and a quantity change
all begin with `orderEditBegin`, all came back `ACCESS_DENIED`, and the only
place that fact existed was a log line nobody was reading. Staff saw the word
`store_unavailable` and pressed the button again.

So the scopes are read once at boot and reported: a missing one is a
deployment fault that no retry will fix, and it should be visible before a
customer asks for something the shop then cannot do.

Reporting only, never repairing. Granting a scope means editing the app's
configuration in Shopify Admin and reinstalling it -- there is no API for it,
which is exactly why this has to be said out loud rather than fixed quietly.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from config.settings import settings

log = logging.getLogger("wanas.shopify.scopes")

#: Never hold boot open on a slow vendor.
TIMEOUT_SECONDS = 10

#: The scopes this codebase actually calls for, each with the feature that
#: stops working without it. Kept short on purpose: a list of everything
#: Shopify offers would be noise, and a scope nobody uses going missing is
#: not a fault.
REQUIRED_SCOPES = {
    "read_products": "the catalog sync and every live price/stock read",
    "write_products": "creating and editing products from the dashboard",
    "read_orders": "reading orders",
    "write_orders": "placing an order, and cancelling one",
    # Every one of these begins with `orderEditBegin`, which is the field
    # Shopify denies, so the three fail together or not at all.
    "write_order_edits": (
        "editing a placed order: approving an item swap, approving an item "
        "add, and changing a line's quantity"
    ),
    "read_inventory": "live stock",
    "write_inventory": "reserving and releasing stock",
    "read_customers": "matching a returning customer",
    "write_customers": "attaching a customer to an order",
}


def granted() -> set[str] | None:
    """The scopes Shopify says this token has, or None if it could not be read.

    `None` is "we do not know", and is deliberately different from the empty
    set: a network blip at boot must not be reported as a shop with no
    permissions at all.
    """
    if not settings.shopify_configured:
        return None
    url = f"https://{settings.shopify_store_domain}/admin/oauth/access_scopes.json"
    try:
        response = httpx.get(
            url,
            headers={"X-Shopify-Access-Token": settings.shopify_admin_token},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            # No credential in the message: the token is in the header above.
            log.warning("could not read the Shopify access scopes (%s)", response.status_code)
            return None
        payload = response.json() or {}
    except Exception:
        log.warning("could not read the Shopify access scopes", exc_info=True)
        return None
    return {str(s.get("handle") or "") for s in payload.get("access_scopes") or []}


#: The last answer, and when. `/health` is polled by the platform every few
#: seconds and this is an outbound HTTP call; a scope grant happens roughly
#: never, so it is read once and then hourly. `refresh()` is the way to force
#: a re-read after granting one, without waiting out the interval.
_CACHE_SECONDS = 3600.0
_lock = threading.Lock()
_cached: tuple[float, list[str]] | None = None


def missing(*, refresh: bool = False) -> list[str]:
    """The required scopes this token does not have.

    Empty when all is well, and also empty when the scopes could not be read
    -- an unknown is not a finding, and reporting one as a fault would be its
    own false alarm. Cached, because this is called from `/health`.
    """
    global _cached
    now = time.monotonic()
    with _lock:
        if not refresh and _cached is not None and now - _cached[0] < _CACHE_SECONDS:
            return list(_cached[1])

    have = granted()
    if have is None:
        # Do not cache a failure: the next call should try again rather than
        # report "all fine" for an hour on the strength of one timeout.
        return []
    gaps = sorted(scope for scope in REQUIRED_SCOPES if scope not in have)
    with _lock:
        _cached = (now, list(gaps))
    return gaps


def log_scope_check() -> list[str]:
    """Report once, at boot, at a level that matches the verdict."""
    gaps = missing(refresh=True)
    if not gaps:
        return gaps
    for scope in gaps:
        log.error(
            "the Shopify Admin token is missing the %s scope. %s will fail with "
            "ACCESS_DENIED until it is granted -- add it to the app in Shopify "
            "Admin and reinstall. Retrying never helps.",
            scope,
            # Capitalised because it opens the second sentence.
            REQUIRED_SCOPES[scope][:1].upper() + REQUIRED_SCOPES[scope][1:],
        )
    return gaps
