"""Shopify Admin API client -- the read path the chatbot serves customers from.

This is deliberately a *different* client from the one inside
`scripts/shopify_sync.py`. That one is a migration tool: it runs once, you read
its diff, and `sys.exit` on an error is the correct behaviour because a human
is watching. This one runs inside a live request while a customer waits, so it
never exits the process and never raises past its own boundary. Every failure
degrades to a documented value -- `ShopifyUnavailable` -- which the calling
service turns into "I can't check stock right now, one moment" rather than a
traceback delivered to WhatsApp.

Variants are addressed by **SKU**, which carries the wanas.db `variant_id`.
Titles are not used for matching anywhere in this module: an admin correcting a
typo in a product name must not make the bot tell a customer the product does
not exist.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request

from config.settings import settings

try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover - certifi is an indirect dependency
    _SSL_CONTEXT = None

log = logging.getLogger("wanas.shopify")

#: Raised for every failure mode -- auth, network, throttle, malformed reply.
#: The services above catch this one name and answer the customer honestly.
class ShopifyUnavailable(RuntimeError):
    pass


class ShopifyConfigError(RuntimeError):
    """Missing domain or token. A startup problem, not a runtime one -- worth
    telling apart from ShopifyUnavailable so it is not retried forever."""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

DEFAULT_API_VERSION = "2025-01"

#: How long a single customer-facing call may take before we give up and say
#: so. A WhatsApp reply that arrives after 30s has already failed socially,
#: so waiting the full 60s the sync script allows itself is pointless here.
REQUEST_TIMEOUT = 8.0

#: Live-per-message reads mean the throttle is a real operational limit, not a
#: theoretical one. Below this many points we slow down deliberately; if the
#: bot starts hitting `_THROTTLE_FLOOR` regularly that is the signal to move to
#: the webhook approach.
_THROTTLE_FLOOR = 200


class ShopifyClient:
    """Minimal GraphQL client, safe to share across threads.

    Retries only on the errors that are actually transient (throttle, 5xx,
    network). A 401 or 403 is a configuration mistake and retrying it just
    makes the customer wait longer for the same failure.
    """

    def __init__(
        self,
        domain: str | None = None,
        token: str | None = None,
        version: str | None = None,
        *,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.domain = domain or settings.shopify_store_domain
        self.token = token or settings.shopify_admin_token
        self.version = version or settings.shopify_api_version or DEFAULT_API_VERSION
        #: Per-instance, not the module constant directly -- `get_admin_client()`
        #: hands bulk list/stats pages a longer budget than a live chat turn
        #: gets, without the two sharing a mutable global.
        self.timeout = timeout
        self._lock = threading.Lock()
        self._throttle_until = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.domain and self.token)

    @property
    def url(self) -> str:
        return f"https://{self.domain}/admin/api/{self.version}/graphql.json"

    # ----------------------------------------------------------------------

    def __call__(self, query: str, variables: dict | None = None) -> dict:
        if not self.configured:
            raise ShopifyConfigError(
                "SHOPIFY_STORE_DOMAIN and SHOPIFY_ADMIN_TOKEN must be set in .env"
            )

        self._respect_throttle()

        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        headers = {
            "X-Shopify-Access-Token": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        payload: dict | None = None
        last_error = "unknown"

        # Three attempts, not six. The sync script can afford to sit through a
        # 32-second backoff; a customer cannot.
        for attempt in range(3):
            req = urllib.request.Request(self.url, data=body, headers=headers)
            try:
                # certifi's bundle, not the OS trust store, which can go
                # stale (seen failing with CERTIFICATE_VERIFY_FAILED on an
                # otherwise-valid chain).
                with urllib.request.urlopen(
                    req, timeout=self.timeout, context=_SSL_CONTEXT
                ) as response:
                    payload = json.loads(response.read().decode())
                break
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode()[:300]
                except Exception:  # pragma: no cover - body already consumed
                    pass
                if exc.code in (401, 403):
                    # Not transient. Surface it loudly in the log and stop.
                    log.error("Shopify rejected the token (HTTP %s): %s", exc.code, detail)
                    raise ShopifyConfigError(
                        f"Shopify returned {exc.code}. Check SHOPIFY_ADMIN_TOKEN and its scopes "
                        "(read_products, read_inventory, read_locations)."
                    ) from exc
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                break
            except urllib.error.URLError as exc:
                last_error = f"network: {exc.reason}"
                if attempt < 2:
                    time.sleep(0.4 * (2 ** attempt))
                    continue
                break
            except (TimeoutError, OSError) as exc:
                last_error = f"timeout: {exc}"
                if attempt < 2:
                    continue
                break
            except json.JSONDecodeError as exc:
                last_error = f"unreadable reply: {exc}"
                break

        if payload is None:
            log.warning("Shopify call failed: %s", last_error)
            raise ShopifyUnavailable(last_error)

        if payload.get("errors"):
            message = json.dumps(payload["errors"])[:400]
            log.warning("Shopify GraphQL error: %s", message)
            raise ShopifyUnavailable(message)

        self._note_cost(payload)

        data = payload.get("data")
        if data is None:
            raise ShopifyUnavailable("Shopify returned no data")
        return data

    # ----------------------------------------------------------------------

    def _respect_throttle(self) -> None:
        """If the last call came back nearly out of credit, pause here rather
        than letting Shopify reject the next one."""
        with self._lock:
            wait = self._throttle_until - time.monotonic()
        if wait > 0:
            time.sleep(min(wait, 2.0))

    def _note_cost(self, payload: dict) -> None:
        cost = (payload.get("extensions") or {}).get("cost") or {}
        status = cost.get("throttleStatus") or {}
        available = status.get("currentlyAvailable")
        if available is None:
            return
        if available < _THROTTLE_FLOOR:
            restore = status.get("restoreRate") or 50
            needed = (_THROTTLE_FLOOR - available) / max(restore, 1)
            log.info("Shopify throttle low (%s points left), pausing %.1fs", available, needed)
            with self._lock:
                self._throttle_until = time.monotonic() + needed


#: One shared client. Cheap to construct, but a single instance means the
#: throttle pause is shared across concurrent conversations instead of each
#: request discovering the limit for itself.
_client: ShopifyClient | None = None
_client_lock = threading.Lock()


def get_client() -> ShopifyClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = ShopifyClient()
        return _client


#: Everything above is tuned for an 8s live chat turn a customer is waiting
#: on. A dashboard product/order list paginates through dozens of calls and
#: can afford to wait longer for each -- `scripts/shopify_sync.py` makes the
#: same call with a 60s timeout for the same reason. A separate instance (not
#: `get_client()` with a mutated timeout) so the two never fight over the same
#: throttle-pause state under concurrent staff + customer traffic.
ADMIN_REQUEST_TIMEOUT = 20.0

_admin_client: ShopifyClient | None = None
_admin_client_lock = threading.Lock()


def get_admin_client() -> ShopifyClient:
    global _admin_client
    with _admin_client_lock:
        if _admin_client is None:
            _admin_client = ShopifyClient(timeout=ADMIN_REQUEST_TIMEOUT)
        return _admin_client
