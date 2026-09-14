"""Live price and stock -- and, where Shopify has one, the photo -- read from
Shopify at the moment a customer asks.

Why this module is a thin overlay rather than a replacement for `catalog.py`:

Shopify owns the facts that change without anyone telling wanas.db -- `price`
and `stock_qty` above all, and those are the two that hurt when they are
wrong. Quoting 650 for a hoodie the customer is charged 700 for, or promising
an XL that the storefront sold four minutes ago, are the failures this exists
to prevent. A product photo does not go stale the same way, but once staff
have uploaded one to Shopify Admin it is the current photo, the same way the
price on the product page is the current price -- so `catalog.get_variants`
prefers it over whatever file `wanas.db` was seeded with.

Everything else a reply needs -- style facets, department, collection, the
size chart, which photo belongs to which colourway when Shopify has not been
given one -- has no home on Shopify and does not drift on its own. Those keep
coming from wanas.db. Moving the whole taxonomy into Shopify tags is a
separate migration and not one the chatbot needs in order to stop overselling.

Variants are matched on **SKU**, which holds the wanas.db `variant_id`. Nothing
here matches on a product title.
"""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

from common import telemetry
from config.settings import settings
from integrations.shopify.client import (
    ShopifyConfigError,
    ShopifyUnavailable,
    get_client,
)

log = logging.getLogger("wanas.shopify.catalog")

#: Pulls every variant with its SKU, price and available quantity in one call.
#: `inventoryQuantity` is the sum across locations, which is what the single
#: Shebeen El-Kom location makes it anyway. `image` / `featuredImage` ride
#: along on the same call rather than a second query -- a photo is read no
#: more often than the price next to it, so it costs nothing extra to ask for
#: both at once.
VARIANTS_QUERY = """
query($cursor: String) {
  productVariants(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      sku
      price
      compareAtPrice
      inventoryQuantity
      inventoryItem { id tracked }
      image { url }
      product { id title status featuredImage { url } }
    }
  }
}
"""

#: Used at confirm_order time for a handful of specific variants. Cheaper and
#: far faster than re-reading the catalog to check three lines.
BY_SKU_QUERY = """
query($query: String!) {
  productVariants(first: 50, query: $query) {
    nodes {
      id
      sku
      price
      compareAtPrice
      inventoryQuantity
      inventoryItem { id tracked }
      image { url }
      product { id title status featuredImage { url } }
    }
  }
}
"""


@dataclass(frozen=True)
class LiveVariant:
    """What Shopify says about one variant right now."""

    variant_id: str          # the wanas.db id, read from the Shopify SKU
    shopify_id: str
    #: What inventory adjustments are addressed to -- not the variant id.
    inventory_item_id: str
    price: Decimal
    original_price: Decimal
    stock_qty: int
    tracked: bool
    product_active: bool
    #: The variant's own photo if staff set one, else the product's featured
    #: photo, else None. A hint for `catalog.get_variants` to prefer over the
    #: local file -- never required, since most demo variants have neither.
    image_url: str | None = None

    @property
    def on_sale(self) -> bool:
        return self.original_price > self.price


def _to_decimal(raw) -> Decimal:
    if raw in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")


def _node_to_live(node: dict) -> LiveVariant | None:
    sku = (node.get("sku") or "").strip()
    if not sku:
        # A variant with no SKU cannot be tied to anything in wanas.db. Guessing
        # by title is exactly the fragility the SKU exists to remove, so it is
        # skipped and reported rather than matched approximately.
        return None

    price = _to_decimal(node.get("price"))
    compare = _to_decimal(node.get("compareAtPrice"))
    product = node.get("product") or {}
    inventory_item = node.get("inventoryItem") or {}
    tracked = bool(inventory_item.get("tracked", True))
    qty = node.get("inventoryQuantity")

    # The variant's own photo first -- a customer picking olive should not see
    # whichever colour happens to be the product's featured image -- and the
    # product's featured photo only for a variant nobody has photographed on
    # its own.
    own_image = (node.get("image") or {}).get("url")
    featured_image = (product.get("featuredImage") or {}).get("url")
    image_url = own_image or featured_image or None

    return LiveVariant(
        variant_id=sku,
        shopify_id=node.get("id", ""),
        inventory_item_id=inventory_item.get("id", ""),
        price=price,
        # compareAtPrice is only set while something is discounted; with no
        # discount the "original" price is the price itself.
        original_price=compare if compare > price else price,
        # An untracked variant is one Shopify will sell without counting. It is
        # not "zero left" -- treating it as sold out would hide it from the bot
        # entirely, so it reads as available.
        stock_qty=(int(qty) if qty is not None else 0) if tracked else 999,
        tracked=tracked,
        product_active=(product.get("status") or "").upper() == "ACTIVE",
        image_url=image_url,
    )


def fetch_all() -> dict[str, LiveVariant]:
    """Every variant in the store, keyed by wanas.db variant_id.

    Raises ShopifyUnavailable / ShopifyConfigError -- callers decide whether to
    fall back or to tell the customer.
    """
    client = get_client()
    out: dict[str, LiveVariant] = {}
    skipped = 0
    cursor = None

    while True:
        data = client(VARIANTS_QUERY, {"cursor": cursor})
        block = data.get("productVariants") or {}
        for node in block.get("nodes") or []:
            live = _node_to_live(node)
            if live is None:
                skipped += 1
                continue
            out[live.variant_id] = live

        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")

    if skipped:
        log.warning(
            "%s Shopify variants have no SKU and were ignored; run "
            "scripts/shopify_set_skus.py --apply",
            skipped,
        )
    return out


def fetch_skus(variant_ids) -> dict[str, LiveVariant]:
    """A named handful, for the stock re-check at order time."""
    wanted = [str(v) for v in variant_ids if v]
    if not wanted:
        return {}

    client = get_client()
    out: dict[str, LiveVariant] = {}

    # Shopify's search syntax caps a practical OR chain well below 250, so the
    # lookup is chunked. Orders are small; this is almost always one call.
    for start in range(0, len(wanted), 20):
        chunk = wanted[start : start + 20]
        query = " OR ".join(f"sku:{sku}" for sku in chunk)
        data = client(BY_SKU_QUERY, {"query": query})
        for node in (data.get("productVariants") or {}).get("nodes") or []:
            live = _node_to_live(node)
            if live is not None and live.variant_id in chunk:
                out[live.variant_id] = live

    return out


def try_fetch_one(variant_id: str) -> LiveVariant | None:
    """A single fresh variant, None if it cannot be read.

    For the places that want Shopify's number if it is available and can
    reasonably fall back to the local row -- not for anything that decides
    whether a sale may happen.
    """
    try:
        return fetch_skus([variant_id]).get(variant_id)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        log.warning("Could not read %s from Shopify: %s", variant_id, exc)
        return None


def try_fetch_all() -> dict[str, LiveVariant] | None:
    """`fetch_all` that answers None instead of raising.

    For the browse path, where wanas.db's own numbers are a reasonable last
    resort. Not for the order path -- see `services/orders.py`, which must know
    the difference between "in stock" and "could not check".
    """
    try:
        return fetch_all()
    except ShopifyConfigError as exc:
        log.error("Shopify not configured, serving wanas.db prices: %s", exc)
        return None
    except ShopifyUnavailable as exc:
        log.warning("Shopify unreachable, serving wanas.db prices: %s", exc)
        return None


# --------------------------------------------------------------------------
# per-turn scope
# --------------------------------------------------------------------------
#
# "Live per message" means live per *message*, not per tool call. One reply can
# easily run get_categories, get_products and get_variants; that is one thing
# the customer asked, and it should cost one call to Shopify, not three. The
# result is held for the duration of the turn and thrown away after it, so the
# next message reads the shelf again.
#
# A ContextVar rather than a module global: two conversations handled
# concurrently must not see each other's snapshot, and a stale snapshot leaking
# across turns is exactly the staleness this work exists to remove.

_UNSET = object()

#: Where `prefetch` runs the read. Small: it is one short HTTP call per turn,
#: and the dispatcher already caps how many turns run at once.
_prefetch_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wanas-shelf")

_turn_cache: contextvars.ContextVar = contextvars.ContextVar("wanas_shopify_turn", default=_UNSET)
#: Whether a turn is actually open. Without this, caching outside one would
#: never expire -- there would be no `reset` to end it -- and the first read
#: taken by the dashboard or an API call would be served to every later caller
#: for the life of the process.
_in_turn: contextvars.ContextVar = contextvars.ContextVar("wanas_shopify_in_turn", default=False)


#: The read started before anything asked for it, so the first model hop pays
#: for it instead of the tool that needs it. Same ContextVar discipline as the
#: cache above: a future belongs to one turn on one thread.
_turn_future: contextvars.ContextVar = contextvars.ContextVar(
    "wanas_shopify_turn_future", default=None
)


@contextmanager
def turn_scope():
    """Wraps the handling of one inbound message."""
    cache_token = _turn_cache.set(_UNSET)
    turn_token = _in_turn.set(True)
    future_token = _turn_future.set(None)
    try:
        yield
    finally:
        _turn_cache.reset(cache_token)
        _in_turn.reset(turn_token)
        _turn_future.reset(future_token)


def prefetch() -> None:
    """Start this turn's live read now, so the model hop pays for it.

    The shelf read is ~440 ms from the Railway container, and it used to be
    spent where the first catalog tool asked for it -- which is *after* the
    model has already come back saying which tool to call, i.e. squarely on
    the critical path. Started here it overlaps the first model round trip
    (~2.4 s), which is several times longer, so by the time any tool wants the
    snapshot it is already sitting there.

    This is not a cache and deliberately not one. The snapshot is still read
    once per message and thrown away with the turn, so "live per message"
    means exactly what it meant before -- which matters, because
    `catalog.live_stock` reads through this and `add_to_cart` decides whether
    a sale may happen on what it says. A 30-second cache would have been
    cheaper and would have let a sold-out size be sold.

    What it does cost is a Shopify call on turns that would never have made
    one -- a greeting, a thank-you. At this shop's volume that is far below
    anything `_respect_throttle` reacts to, but it is a real doubling and
    `SHOPIFY_PREFETCH=0` is the way out of it.
    """
    if not settings.shopify_prefetch or _turn_cache.get() is not _UNSET:
        return
    if _turn_future.get() is not None:
        return
    try:
        _turn_future.set(_prefetch_pool.submit(try_fetch_all))
    except RuntimeError:  # pragma: no cover - pool shutting down
        pass


def live_map() -> dict[str, LiveVariant] | None:
    """The store's live prices and stock, fetched at most once per turn.

    None means Shopify could not be reached and the caller should fall back to
    wanas.db. A failure is cached for the turn too -- retrying a dead endpoint
    three times inside one reply only makes the customer wait longer.
    """
    cached = _turn_cache.get()
    if cached is not _UNSET:
        return cached

    # `prefetch` may have started this read when the turn opened. Almost always
    # finished by now -- the model hop it was overlapped with is several times
    # longer -- so collecting it is not a wait. `try_fetch_all` answers None
    # rather than raising either way, so nothing new can come out of here that
    # could not come out of calling it directly.
    future = _turn_future.get()
    if future is None:
        result = try_fetch_all()
    else:
        # Timed separately and deliberately. A prefetched read happens on
        # another thread, so it leaves the turn's line with no `shopify` stage
        # at all -- which reads as "the shelf was free" when what actually
        # happened is "the shelf was paid for somewhere else". `shopify_wait`
        # is the honest number: how long this turn stood still for it, which
        # should be close to zero and is the thing to watch if it stops being.
        with telemetry.stage("shopify_wait"):
            result = future.result()
    if _in_turn.get():
        # Only inside a turn. Outside one -- the dashboard, a script, a test --
        # there is nothing that will ever clear it, so caching would hand a
        # frozen snapshot to every later caller.
        _turn_cache.set(result)
    return result


def prime(value: dict[str, LiveVariant] | None) -> None:
    """Inject a snapshot -- used by the verification script and tests so they
    can compare both sides without a second round trip."""
    _turn_cache.set(value)
