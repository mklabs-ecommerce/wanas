"""Product management for the staff dashboard: list, detail, create, edit.

Shopify owns price, stock and the variant list; wanas.db owns `category`,
`department`, `style`, `collection` and `size_chart` -- fields Shopify has no
place for (see `domain/services/catalog.py`'s module docstring). A product
created only on Shopify through this module would be invisible to the bot's
own search, which reads those fields from Postgres -- so every write here
pushes to Shopify first (the source of truth) and then mirrors the result
into local `Product` / `Variant`, using the same variant_id/SKU convention
`domain/seed/products.py` already writes.

**No new column stores a Shopify product id.** Nothing in this codebase
matches a product by anything other than SKU (`shopify_catalog.py`'s own
docstring: "nothing here matches on a product title"). Editing an existing
product resolves its Shopify product id fresh, from any one of its local
variant SKUs, rather than adding a second, potentially-stale place that fact
could live.

Removing a product or one of its variants *is* supported, and the story it
used to lack is the delete/archive split near the bottom of this file: an
order line points at a variant row, so anything that has ever been sold is
archived rather than deleted. Read that section before changing either.
"""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from domain.models import CartItem, OrderItem, Product, StockWaitlistEntry, Variant
from integrations.shopify import (
    catalog as shopify_catalog,
    inventory as shopify_inventory,
    size_charts as shopify_size_charts,
)
from integrations.shopify.client import (
    ShopifyConfigError,
    ShopifyUnavailable,
    get_admin_client,
)

log = logging.getLogger("wanas.shopify.admin_products")

PAGE_SIZE = 25

#: How long to wait for Shopify to finish processing an uploaded picture
#: before mirroring the product locally without its url.
MEDIA_POLL_TRIES = 6
MEDIA_POLL_DELAY = 0.6

PRODUCTS_QUERY = """
query($cursor: String, $query: String) {
  products(first: 25, after: $cursor, query: $query) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      status
      productType
      descriptionHtml
      featuredImage { url }
      variants(first: 100) {
        nodes {
          id
          sku
          price
          compareAtPrice
          inventoryQuantity
          image { url }
          selectedOptions { name value }
        }
      }
    }
  }
}
"""

PRODUCT_TYPES = """
{ shop { productTypes(first: 100) { edges { node } } } }
"""

PRODUCT_DETAIL_BY_SKU = """
query($query: String!) {
  productVariants(first: 1, query: $query) {
    nodes { product { id } }
  }
}
"""

#: `productCreate` takes a `ProductCreateInput` under `product`, and that is
#: the only place a new product's options can be declared. Handing them to
#: `productUpdate` afterwards leaves the product with the default `Title`
#: option Shopify made it with, and every real variant is then refused with
#: "Option does not exist".
PRODUCT_CREATE = """
mutation($product: ProductCreateInput!) {
  productCreate(product: $product) {
    product { id title }
    userErrors { field message }
  }
}
"""

#: `REMOVE_STANDALONE_VARIANT` takes the placeholder variant Shopify creates
#: every product with away as the real ones land. Without it the product keeps
#: a phantom "Default Title" variant with no SKU, which the bot would read as
#: a size nobody can order.
VARIANTS_CREATE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(
    productId: $productId
    variants: $variants
    strategy: REMOVE_STANDALONE_VARIANT
  ) {
    productVariants { id sku inventoryItem { id } }
    userErrors { field message }
  }
}
"""

VARIANTS_UPDATE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku }
    userErrors { field message }
  }
}
"""

#: A new product is not on any sales channel. `status: ACTIVE` only means "not
#: a draft"; until it is *published* to the Online Store it has no
#: `publishedAt`, no storefront url, and does not appear in a collection on
#: the site. The 18 products already on the shelf were published by hand in
#: Admin, which is why nobody noticed.
PUBLICATIONS = """
{ publications(first: 25) { nodes { id name } } }
"""

PUBLISH = """
mutation($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}
"""

PRODUCT_DELETE = """
mutation($input: ProductDeleteInput!) {
  productDelete(input: $input) {
    deletedProductId
    userErrors { field message }
  }
}
"""

VARIANTS_DELETE = """
mutation($productId: ID!, $variantsIds: [ID!]!) {
  productVariantsBulkDelete(productId: $productId, variantsIds: $variantsIds) {
    userErrors { field message }
  }
}
"""

COLLECTION_ADD = """
mutation($id: ID!, $productIds: [ID!]!) {
  collectionAddProducts(id: $id, productIds: $productIds) {
    userErrors { field message }
  }
}
"""

PRODUCT_UPDATE = """
mutation($input: ProductInput!) {
  productUpdate(input: $input) { userErrors { field message } }
}
"""

MEDIA_CREATE = """
mutation($productId: ID!, $media: [CreateMediaInput!]!) {
  productCreateMedia(productId: $productId, media: $media) {
    media {
      id
      status
      ... on MediaImage { image { url } }
    }
    mediaUserErrors { field message }
  }
}
"""

#: A picture Shopify has only just been handed has no url yet -- it answers
#: `status: UPLOADED` and a null image. The gid is enough for Shopify itself
#: (it is what a variant's `mediaId` points at), but the local mirror stores
#: urls, so this reads them back once processing has caught up.
PRODUCT_MEDIA = """
query($id: ID!) {
  product(id: $id) {
    media(first: 50) {
      nodes {
        id
        ... on MediaImage { image { url } }
      }
    }
  }
}
"""

INVENTORY_SET = """
mutation($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    userErrors { field message }
  }
}
"""

INVENTORY_LEVELS = """
query($ids: [ID!]!, $location: ID!) {
  nodes(ids: $ids) {
    ... on InventoryItem {
      id
      inventoryLevel(locationId: $location) {
        quantities(names: ["available"]) { name quantity }
      }
    }
  }
}
"""


class ProductRejected(RuntimeError):
    """Shopify refused the write and said why -- a bad request, not an outage."""


def _is_stale_compare(errors: list[dict]) -> bool:
    """Shopify refused because the shelf moved between our read and our write."""
    for error in errors:
        message = (error.get("message") or "").lower()
        if "changefromquantity" in message.replace(" ", "") or "quantity has changed" in message:
            return True
        if "compare" in message and "quantity" in message:
            return True
    return False


def _errors(block: dict | None, key: str) -> None:
    errors = (block or {}).get("userErrors") or (block or {}).get("mediaUserErrors") or []
    if errors:
        message = "; ".join(e.get("message", "") for e in errors)
        log.warning("Shopify rejected %s: %s", key, message)
        raise ProductRejected(message)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug or "item"


def _unique_product_id(session: Session, title: str) -> str:
    base = slugify(title)
    candidate = base
    n = 2
    while session.get(Product, candidate) is not None:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _variant_id(product_id: str, size: str, color: str | None, length: str | None) -> str:
    parts = [product_id, slugify(size)]
    if color:
        parts.append(slugify(color))
    if length:
        parts.append(slugify(length))
    return "-".join(parts)


def _summarize(variants: list[dict]) -> dict:
    prices = [Decimal(str(v["price"])) for v in variants]
    originals = [Decimal(str(v.get("original_price", v["price"]))) for v in variants]
    return {
        "sizes": sorted({v["size"] for v in variants if v.get("size")}),
        "colors": sorted({v["color"] for v in variants if v.get("color")}),
        "lengths": sorted({v["length"] for v in variants if v.get("length")}),
        "price": min(prices) if prices else Decimal("0"),
        "original_price": max(originals) if originals else Decimal("0"),
        "on_sale": any(o > p for o, p in zip(originals, prices, strict=True)),
    }


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------


def _product_summary(node: dict) -> dict:
    variants = (node.get("variants") or {}).get("nodes") or []
    return {
        "id": node["id"],
        "title": node.get("title"),
        "status": node.get("status"),
        "category": node.get("productType"),
        "image_url": (node.get("featuredImage") or {}).get("url"),
        "variant_count": len(variants),
        "any_in_stock": any((v.get("inventoryQuantity") or 0) > 0 for v in variants),
    }


def list_products(*, query: str | None = None, cursor: str | None = None) -> dict:
    client = get_admin_client()
    data = client(PRODUCTS_QUERY, {"cursor": cursor, "query": query})
    block = data.get("products") or {}
    page = block.get("pageInfo") or {}
    return {
        "products": [_product_summary(n) for n in block.get("nodes") or []],
        "has_next_page": bool(page.get("hasNextPage")),
        "end_cursor": page.get("endCursor"),
    }


def _opt(variant: dict, name: str) -> str | None:
    for o in variant.get("selectedOptions") or []:
        if o.get("name", "").lower() == name.lower():
            return o.get("value")
    return None


def get_product(shopify_gid: str) -> dict | None:
    """A single product's detail, re-using the list query's shape (Shopify has
    no single-product-by-id-with-variants shortcut cheaper than this)."""
    client = get_admin_client()
    data = client(PRODUCTS_QUERY, {"cursor": None, "query": f"id:{shopify_gid.rsplit('/', 1)[-1]}"})
    nodes = (data.get("products") or {}).get("nodes") or []
    node = next((n for n in nodes if n["id"] == shopify_gid), None)
    if node is None:
        return None

    out = _product_summary(node)
    out["description_html"] = node.get("descriptionHtml")
    out["variants"] = [
        {
            "id": v["id"],
            "sku": v.get("sku"),
            "price": v.get("price"),
            "compare_at_price": v.get("compareAtPrice"),
            "inventory_quantity": v.get("inventoryQuantity"),
            # The colourway's own photo, so the row that says "Navy" can be
            # checked against the picture Shopify will actually send for it.
            "image_url": (v.get("image") or {}).get("url"),
            "size": _opt(v, "Size"),
            "color": _opt(v, "Color"),
            "length": _opt(v, "Length"),
        }
        for v in (node.get("variants") or {}).get("nodes") or []
    ]
    return out


def product_types() -> list[str]:
    """Every `productType` the shop already uses, sorted."""
    client = get_admin_client()
    data = client(PRODUCT_TYPES)
    edges = (((data.get("shop") or {}).get("productTypes") or {}).get("edges")) or []
    return sorted({e["node"] for e in edges if e.get("node")}, key=str.casefold)


def product_gid_for_variant_id(variant_id: str) -> str | None:
    """The Shopify product gid that owns the variant with this SKU.

    The only way any code in this codebase is allowed to find a Shopify
    product: through a variant SKU it already trusts, never a stored id.
    """
    client = get_admin_client()
    data = client(PRODUCT_DETAIL_BY_SKU, {"query": f"sku:{variant_id}"})
    nodes = (data.get("productVariants") or {}).get("nodes") or []
    if not nodes:
        return None
    return (nodes[0].get("product") or {}).get("id")


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def _build_options(variants: list[dict]) -> list[dict]:
    """`productOptions` for `productUpdate`.

    An option value is an `OptionValueCreateInput`, not a string: Shopify
    refuses the whole document with "Expected \"L\" to be a key-value object"
    for a bare list, which is what every product created here got.
    """

    def values(names: set[str]) -> list[dict]:
        return [{"name": n} for n in sorted(names)]

    options = [{"name": "Size", "values": values({v["size"] for v in variants})}]
    colors = {v["color"] for v in variants if v.get("color")}
    if colors:
        options.append({"name": "Color", "values": values(colors)})
    lengths = {v["length"] for v in variants if v.get("length")}
    if lengths:
        options.append({"name": "Length", "values": values(lengths)})
    return options


def _option_values(variant: dict) -> list[dict]:
    values = [{"optionName": "Size", "name": variant["size"]}]
    if variant.get("color"):
        values.append({"optionName": "Color", "name": variant["color"]})
    if variant.get("length"):
        values.append({"optionName": "Length", "name": variant["length"]})
    return values


#: Everything below that actually talks to Shopify is factored into small,
#: separately named functions rather than inlined into `create_product` /
#: `update_product`. That is the same boundary `shopify_orders.py` draws
#: around `create_order` / `set_line_quantity`: the *orchestration* (which
#: calls happen in which order, and the local-DB mirror at the end) is real
#: code every test exercises; only the network-calling primitives are what a
#: fake replaces -- so a test never has to reimplement the mirroring logic to
#: exercise it.


def shopify_create_product(
    *,
    title: str,
    description: str,
    category: str,
    options: list[dict] | None = None,
    vendor: str | None = None,
) -> str:
    """Returns the new product's Shopify id.

    `options` is the whole option set -- Size, and Color/Length where the
    variants have them. It goes in here rather than in a `productUpdate`
    afterwards: see `PRODUCT_CREATE`.
    """
    client = get_admin_client()
    created = client(
        PRODUCT_CREATE,
        {
            "product": {
                "title": title,
                "descriptionHtml": description or "",
                "productType": category,
                # Shopify defaults this to the *store's* name, which is not
                # what the products already on the shelf say.
                "vendor": vendor or settings.shopify_vendor,
                "status": "ACTIVE",
                "productOptions": options or [],
            }
        },
    )
    _errors(created.get("productCreate"), "productCreate")
    return (created["productCreate"]["product"] or {})["id"]


def shopify_create_variants(product_gid: str, bulk_input: list[dict]) -> list[dict]:
    """Returns `[{"sku", "inventory_item_id"}]` for each variant created."""
    client = get_admin_client()
    result = client(VARIANTS_CREATE, {"productId": product_gid, "variants": bulk_input})
    _errors(result.get("productVariantsBulkCreate"), "productVariantsBulkCreate")
    nodes = result["productVariantsBulkCreate"]["productVariants"]
    return [
        {"id": n["id"], "sku": n["sku"], "inventory_item_id": n["inventoryItem"]["id"]}
        for n in nodes
    ]


def shopify_attach_media(product_gid: str, image_url: str) -> None:
    shopify_attach_images(product_gid, [{"source": image_url}])


def shopify_attach_images(product_gid: str, images: list[dict]) -> list[dict]:
    """Attach pictures to a product, in the order given.

    `images` is `[{"source", "alt"?}]` -- `source` is any url Shopify can
    fetch, which covers both a link somebody pasted and the resource url
    `files.stage` hands back for bytes uploaded off a staff member's laptop.

    Returns `[{"id", "url"}]` lined up with the input, so the caller can point
    a variant at its own colourway's picture (`id`) and mirror the picture
    locally (`url`). `url` is None while Shopify is still processing.
    """
    if not images:
        return []
    client = get_admin_client()
    result = client(
        MEDIA_CREATE,
        {
            "productId": product_gid,
            "media": [
                {
                    "originalSource": image["source"],
                    "alt": image.get("alt") or "",
                    "mediaContentType": "IMAGE",
                }
                for image in images
            ],
        },
    )
    _errors(result.get("productCreateMedia"), "productCreateMedia")
    nodes = (result["productCreateMedia"] or {}).get("media") or []
    out = [{"id": n.get("id"), "url": ((n.get("image") or {}).get("url")) or None} for n in nodes]

    if any(entry["url"] is None for entry in out):
        for entry, url in zip(out, _media_urls(client, product_gid, [e["id"] for e in out]), strict=True):
            entry["url"] = entry["url"] or url
    return out


def _media_urls(client, product_gid: str, media_ids: list[str]) -> list[str | None]:
    """The url of each of `media_ids`, waiting briefly for Shopify to process.

    Same short, bounded wait as `files.poll_url` and for the same reason: a
    staff member is watching a spinner, and a missing url costs the local
    mirror one picture, not the product.
    """
    for attempt in range(MEDIA_POLL_TRIES):
        if attempt:
            time.sleep(MEDIA_POLL_DELAY)
        data = client(PRODUCT_MEDIA, {"id": product_gid})
        nodes = ((data.get("product") or {}).get("media") or {}).get("nodes") or []
        by_id = {n.get("id"): ((n.get("image") or {}).get("url")) or None for n in nodes}
        urls = [by_id.get(mid) for mid in media_ids]
        if all(urls):
            return urls
    log.info("Shopify is still processing media for %s; mirroring what it gave us", product_gid)
    return urls


def shopify_assign_variant_media(product_gid: str, pairs: list[dict]) -> None:
    """Give each variant its colourway's picture. `pairs` is `[{"id", "media_id"}]`.

    This is the whole point of asking for an image per colour rather than one
    per product: `shopify_catalog.LiveVariant.image_url` reads the variant's
    own image, and that is what the bot sends when a customer asks about the
    olive one.
    """
    bulk = [{"id": p["id"], "mediaId": p["media_id"]} for p in pairs if p.get("media_id")]
    if not bulk:
        return
    shopify_update_variants(product_gid, bulk)


def _available_now(client, item_ids: list[str]) -> dict[str, int]:
    """What Shopify currently has at the shop's one location, per item."""
    location = shopify_inventory.location_id()
    current: dict[str, int] = {}
    for i in range(0, len(item_ids), 100):
        chunk = item_ids[i : i + 100]
        data = client(INVENTORY_LEVELS, {"ids": chunk, "location": location})
        for node in data.get("nodes") or []:
            if not node or not node.get("id"):
                continue
            level = node.get("inventoryLevel") or {}
            available = 0
            for entry in level.get("quantities") or []:
                if entry.get("name") == "available":
                    available = int(entry.get("quantity") or 0)
            current[node["id"]] = available
    return current


def shopify_set_inventory(quantities: list[dict], *, _retries: int = 1) -> None:
    """`quantities` is `[{"inventory_item_id", "quantity"}]`.

    A staff-driven stock correction is not racing a concurrent sale the way
    the order path is, so unlike `shopify_inventory.py` this deliberately
    wants the last word rather than a compare-and-swap. Shopify no longer
    offers that as a choice: `ignoreCompareQuantity` is gone from
    `InventorySetQuantitiesInput` (sending it fails the whole document as an
    invalid variable, which is how a saved quantity died in production), and
    `changeFromQuantity` is *required* (as is an `@idempotent` key). So the
    compare is satisfied rather than skipped -- read what is there, set
    against it, and on the rare genuine race read again and re-apply, because
    the number the staff member counted on the shelf is still the right
    answer.
    """
    if not quantities:
        return
    client = get_admin_client()
    shopify_inventory.require_write_api(client.version)
    current = _available_now(client, [q["inventory_item_id"] for q in quantities])
    payload = [
        {
            "inventoryItemId": q["inventory_item_id"],
            "locationId": shopify_inventory.location_id(),
            "quantity": q["quantity"],
            shopify_inventory.COMPARE_FIELD: current.get(q["inventory_item_id"], 0),
        }
        for q in quantities
    ]
    result = client(
        INVENTORY_SET,
        {
            "input": {
                "name": "available",
                "reason": "correction",
                "quantities": payload,
            },
            # One per attempt: it makes the client's retry of a throttled
            # write a replay rather than a second correction, and the
            # re-read below mints a fresh one for its fresh numbers.
            "key": shopify_inventory.idempotency_key(),
        },
    )
    errors = (result.get("inventorySetQuantities") or {}).get("userErrors") or []
    if errors and _retries > 0 and _is_stale_compare(errors):
        log.info("Stock moved under the correction; re-reading and re-applying")
        shopify_set_inventory(quantities, _retries=_retries - 1)
        return
    _errors(result.get("inventorySetQuantities"), "inventorySetQuantities")


def shopify_update_product_fields(product_gid: str, fields: dict) -> None:
    client = get_admin_client()
    result = client(PRODUCT_UPDATE, {"input": {"id": product_gid, **fields}})
    _errors(result.get("productUpdate"), "productUpdate")


def shopify_update_variants(product_gid: str, bulk_input: list[dict]) -> None:
    client = get_admin_client()
    result = client(VARIANTS_UPDATE, {"productId": product_gid, "variants": bulk_input})
    _errors(result.get("productVariantsBulkUpdate"), "productVariantsBulkUpdate")


def shopify_publish_to_online_store(product_gid: str) -> str | None:
    """Put the product on the Online Store sales channel.

    Returns None on success, or a sentence explaining why not -- this is the
    one step in the create path that is allowed to fail without failing the
    product. The app needs `read_publications` and `write_publications` for
    it, and a shop whose token predates that gets a product that exists,
    sells through the bot, and is simply not on the website yet. Refusing the
    whole creation over it would be worse; saying nothing would be worse
    still, which is why the message travels back to the dashboard.
    """
    client = get_admin_client()
    try:
        data = client(PUBLICATIONS)
        nodes = (data.get("publications") or {}).get("nodes") or []
        online = next((n for n in nodes if n.get("name") == "Online Store"), None)
        if online is None:
            return "this shop has no Online Store sales channel to publish to"
        result = client(PUBLISH, {"id": product_gid, "input": [{"publicationId": online["id"]}]})
        errors = (result.get("publishablePublish") or {}).get("userErrors") or []
        if errors:
            return "; ".join(e.get("message", "") for e in errors)
    except ShopifyUnavailable as exc:
        if "ACCESS_DENIED" in str(exc) or "access scope" in str(exc):
            log.warning("cannot publish %s: the app has no publications scope", product_gid)
            return (
                "the product is not on the website yet: add the read_publications "
                "and write_publications scopes to the Shopify app, then publish it "
                "by hand this once"
            )
        raise
    return None


def shopify_add_to_collection(collection_gid: str, product_gid: str) -> str | None:
    """Add one product to a manual collection. Returns None, or why not.

    A smart collection's membership is its rules' business -- Shopify refuses
    a manual member, and the right answer is the product's `productType`, not
    a call here.
    """
    client = get_admin_client()
    try:
        result = client(COLLECTION_ADD, {"id": collection_gid, "productIds": [product_gid]})
    except ShopifyUnavailable as exc:
        return str(exc)
    errors = (result.get("collectionAddProducts") or {}).get("userErrors") or []
    return "; ".join(e.get("message", "") for e in errors) or None


def _colour_key(color: str | None) -> str:
    """One spelling of a colour name, so "Camel Brown" and "camel brown" are
    the same colourway. The same normalisation `assistant/tools/base.py` uses
    when it picks which photo to send."""
    return " ".join((color or "").split()).casefold()


def _media_by_color(images: list[dict], attached: list[dict]) -> dict[str, dict]:
    """`{colour key: media}`, first picture wins for a colour.

    A staff member fills one image field per variant row, so a product with
    three sizes in Navy arrives with the same colour three times. The first
    one is the colour's picture; the rest are still attached to the product,
    they just do not overwrite it.
    """
    out: dict[str, dict] = {}
    for image, media in zip(images, attached, strict=False):
        if media.get("id"):
            out.setdefault(_colour_key(image.get("color")), media)
    return out


def _media_for_color(media_by_color: dict[str, dict], color: str | None) -> dict | None:
    """The picture that belongs to one colourway.

    An unlabelled picture stands in only when *no* colour has one of its own.
    Otherwise a product where Navy has a photo and Olive does not would show
    the Navy photo on the Olive variant -- confidently, and wrongly, which is
    the failure `_candidate_images` exists to avoid on the other side.
    """
    key = _colour_key(color)
    if key in media_by_color:
        return media_by_color[key]
    if len(media_by_color) == 1 and "" in media_by_color:
        return media_by_color[""]
    return None


def create_product(
    session: Session,
    *,
    title: str,
    description: str,
    category: str,
    department: str,
    style: list[str] | None,
    collection: str | None,
    size_chart: str | None,
    variants: list[dict],
    image_url: str | None = None,
    images: list[dict] | None = None,
    size_chart_file_gid: str | None = None,
    size_chart_url: str | None = None,
    collection_gid: str | None = None,
) -> dict:
    """Create on Shopify, then mirror into wanas.db.

    `variants` is `[{"size", "color"?, "length"?, "price", "original_price"?,
    "stock_qty"}]`. Raises `ProductRejected` for a Shopify-side refusal (a bad
    field, a duplicate SKU) and `ShopifyUnavailable`/`ShopifyConfigError` for
    an outage -- the dashboard route tells those apart.

    `images` is `[{"color"?, "source", "alt"?}]`, one picture per colourway
    rather than one per product: each is attached as product media *and* set
    as its colour's variants' own image, which is the field
    `shopify_catalog.LiveVariant.image_url` reads and therefore what decides
    which photo the bot sends when a customer names a colour. `image_url` is
    the older single-picture form and is kept as an unlabelled entry.

    `size_chart_file_gid` points `custom.size_chart` at a file already in
    Shopify Files (`files.upload_to_files`); `size_chart_url` is that same
    file's url, stored locally so the bot can send it without asking Shopify.

    `collection` is the merchandising label the bot's search reads;
    `collection_gid` additionally puts the product *in* that Shopify
    collection, and should only be passed for a manual one -- a smart
    collection's membership follows from `category`, which is its rule.

    The returned dict carries `warnings`: things that did not work and did
    not justify failing the product, each already phrased for a staff member
    to read.
    """
    if not variants:
        raise ProductRejected("a product needs at least one variant")

    product_id = _unique_product_id(session, title)

    product_gid = shopify_create_product(
        title=title,
        description=description,
        category=category,
        options=_build_options(variants),
        vendor=settings.shopify_vendor,
    )

    bulk_input = [
        {
            "optionValues": _option_values(v),
            "price": f"{Decimal(str(v['price'])):.2f}",
            "compareAtPrice": (
                f"{Decimal(str(v['original_price'])):.2f}"
                if v.get("original_price") and Decimal(str(v["original_price"])) > Decimal(str(v["price"]))
                else None
            ),
            # The SKU belongs to the inventory item, not the variant --
            # `ProductVariantsBulkInput` has no `sku` field of its own, and
            # sending one there fails the whole document.
            "inventoryItem": {
                "sku": _variant_id(product_id, v["size"], v.get("color"), v.get("length")),
                "tracked": True,
            },
            "inventoryPolicy": "DENY",
        }
        for v in variants
    ]
    created_variants = shopify_create_variants(product_gid, bulk_input)

    wanted_images = list(images or [])
    if image_url:
        wanted_images.append({"color": None, "source": image_url})
    attached = shopify_attach_images(
        product_gid,
        [
            {"source": i["source"], "alt": " ".join(filter(None, [title, i.get("color")]))}
            for i in wanted_images
        ],
    )

    by_sku = {c["sku"]: c for c in created_variants}
    media_by_color = _media_by_color(wanted_images, attached)
    shopify_assign_variant_media(
        product_gid,
        [
            {
                "id": by_sku[sku]["id"],
                "media_id": (_media_for_color(media_by_color, v.get("color")) or {}).get("id"),
            }
            for v in variants
            if (sku := _variant_id(product_id, v["size"], v.get("color"), v.get("length"))) in by_sku
        ],
    )

    if size_chart_file_gid:
        shopify_size_charts.set_product_chart_image(product_gid, size_chart_file_gid)

    warnings = []
    #: Last, and never fatal: a product that exists and sells through the bot
    #: but is not on the website yet is a much better outcome than no product.
    problem = shopify_publish_to_online_store(product_gid)
    if problem:
        warnings.append(problem)
    if collection_gid:
        problem = shopify_add_to_collection(collection_gid, product_gid)
        if problem:
            warnings.append(f"could not add it to the collection: {problem}")

    quantities = []
    for v in variants:
        sku = _variant_id(product_id, v["size"], v.get("color"), v.get("length"))
        created = by_sku.get(sku)
        if created is None:
            continue
        stock_qty = int(v.get("stock_qty", 0))
        quantities.append({"inventory_item_id": created["inventory_item_id"], "quantity": stock_qty})
    shopify_set_inventory(quantities)

    _mirror_local(
        session,
        product_id=product_id,
        title=title,
        description=description,
        category=category,
        department=department,
        style=style,
        collection=collection,
        size_chart=size_chart,
        variants=variants,
        image_url=image_url,
        images=wanted_images,
        media_by_color=media_by_color,
        size_chart_url=size_chart_url,
    )

    return {"product_id": product_id, "shopify_id": product_gid, "warnings": warnings}


def _mirror_local(
    session: Session,
    *,
    product_id: str,
    title: str,
    description: str,
    category: str,
    department: str,
    style: list[str] | None,
    collection: str | None,
    size_chart: str | None,
    variants: list[dict],
    image_url: str | None,
    images: list[dict] | None = None,
    media_by_color: dict[str, dict] | None = None,
    size_chart_url: str | None = None,
) -> Product:
    summary = _summarize(variants)
    product = session.get(Product, product_id)
    if product is None:
        product = Product(product_id=product_id)
        session.add(product)

    product.name = title
    product.category = category
    product.department = department
    product.style = list(style or [])
    product.collection = collection
    product.size_chart = size_chart
    product.sizes = summary["sizes"]
    product.colors = summary["colors"]
    product.lengths = summary["lengths"]
    product.price = summary["price"]
    product.original_price = summary["original_price"]
    product.on_sale = summary["on_sale"]
    gallery = [i["url"] for i in (media_by_color or {}).values() if i.get("url")]
    product.images = gallery or ([image_url] if image_url else list(product.images or []))
    product.color_images = {
        next(i["color"] for i in (images or []) if _colour_key(i.get("color")) == key): [media["url"]]
        for key, media in (media_by_color or {}).items()
        if key and media.get("url")
    } or (product.color_images or {})
    if size_chart_url is not None:
        product.size_chart_image = size_chart_url
    product.description = description or ""
    product.source_products = product.source_products or []

    for v in variants:
        variant_id = _variant_id(product_id, v["size"], v.get("color"), v.get("length"))
        variant = session.get(Variant, variant_id)
        if variant is None:
            variant = Variant(variant_id=variant_id)
            session.add(variant)
        variant.product_id = product_id
        variant.size = v["size"]
        variant.color = v.get("color")
        variant.length = v.get("length")
        variant.price = Decimal(str(v["price"]))
        variant.original_price = Decimal(str(v.get("original_price") or v["price"]))
        variant.on_sale = variant.original_price > variant.price
        variant.stock_qty = int(v.get("stock_qty", 0))
        variant.low_stock_threshold = int(v.get("low_stock_threshold", 2))

    session.flush()
    return product


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------


def _replace_variant_images(
    session: Session, product: Product, product_gid: str, variant_images: list[dict]
) -> None:
    """Attach each new picture and point its whole colourway at it.

    The variants are resolved *before* anything is uploaded. Attaching first
    and discovering afterwards that the row does not belong to this product
    leaves a picture on the product that nothing references and nothing here
    can remove.
    """
    wanted = []
    for image in variant_images:
        variant = session.get(Variant, image["variant_id"])
        if variant is None or variant.product_id != product.product_id:
            log.info("ignoring a photo for %r, which is not this product's", image["variant_id"])
            continue
        wanted.append((image, variant))
    if not wanted:
        return

    attached = shopify_attach_images(
        product_gid,
        [{"source": image["source"], "alt": product.name or ""} for image, _ in wanted],
    )

    #: Every variant that shares a colour with the row the picture was set on
    #: -- and, for a product nobody split by colour, just that row.
    targets: dict[str, str] = {}
    color_urls: dict[str, str] = {}
    for (_, variant), media in zip(wanted, attached, strict=False):
        if not media.get("id"):
            continue
        if variant.color:
            siblings = [v for v in product.variants if v.color == variant.color]
            if media.get("url"):
                color_urls[variant.color] = media["url"]
        else:
            siblings = [variant]
        for sibling in siblings:
            targets[sibling.variant_id] = media["id"]

    if not targets:
        return

    live_by_id = shopify_catalog.fetch_skus(list(targets))
    shopify_assign_variant_media(
        product_gid,
        [
            {"id": live.shopify_id, "media_id": targets[variant_id]}
            for variant_id, live in live_by_id.items()
            if live is not None
        ],
    )

    #: The local fallback for when Shopify is unreachable. Additive: a colour
    #: nobody changed keeps the photo it had.
    if color_urls:
        product.color_images = {
            **(product.color_images or {}),
            **{c: [u] for c, u in color_urls.items()},
        }
    fresh = list(color_urls.values())
    if fresh:
        product.images = fresh + [u for u in (product.images or []) if u not in fresh]


# --------------------------------------------------------------------------
# delete / archive
# --------------------------------------------------------------------------
#
# This module used to say removing a variant was deliberately left to Shopify
# Admin. It is here now because staff asked for it, and the thing that made it
# risky is handled explicitly rather than avoided: `order_items.variant_id` is
# a foreign key, so a variant that has ever been sold cannot have its row
# taken away without taking the order line with it. An order is the record
# that money changed hands; it outranks tidying the catalog.
#
# So there are two verbs, and which one applies is a fact about the data, not
# a choice:
#
#   delete   nothing was ever ordered from it -- it really goes, on both sides
#   archive  it sold before -- Shopify's `status: ARCHIVED` and our own
#            `Product.archived`, which together mean the bot will not offer
#            it, the storefront will not show it, and the orders still read.


class ProductInUse(RuntimeError):
    """Something has been sold from this, so its rows have to stay."""


def _sold_variant_ids(session: Session, variant_ids: list[str]) -> set[str]:
    if not variant_ids:
        return set()
    rows = session.scalars(
        select(OrderItem.variant_id).where(OrderItem.variant_id.in_(variant_ids))
    ).all()
    return set(rows)


def _forget_variant(session: Session, variant_id: str) -> None:
    """Drop the rows that only point at a variant, never at an order.

    A cart line and a back-in-stock waitlist entry are both "somebody is
    waiting for this"; with the variant gone there is nothing to wait for, and
    leaving them would break the same foreign key from the other side.
    """
    for model in (CartItem, StockWaitlistEntry):
        for row in session.scalars(select(model).where(model.variant_id == variant_id)).all():
            session.delete(row)


def shopify_delete_product(product_gid: str) -> None:
    client = get_admin_client()
    result = client(PRODUCT_DELETE, {"input": {"id": product_gid}})
    _errors(result.get("productDelete"), "productDelete")


def shopify_delete_variants(product_gid: str, variant_gids: list[str]) -> None:
    client = get_admin_client()
    result = client(VARIANTS_DELETE, {"productId": product_gid, "variantsIds": variant_gids})
    _errors(result.get("productVariantsBulkDelete"), "productVariantsBulkDelete")


def delete_product(session: Session, product_id: str) -> dict:
    """Remove a product from Shopify and from wanas.db.

    Raises `ProductInUse` when an order references any of its variants -- the
    caller should offer `archive_product` instead, which is the same intent
    without destroying the order lines.
    """
    product = session.get(Product, product_id)
    if product is None:
        return {"error": "product_not_found"}

    variant_ids = [v.variant_id for v in product.variants]
    sold = _sold_variant_ids(session, variant_ids)
    if sold:
        raise ProductInUse(
            f"{len(sold)} of its sizes have been ordered before; archive it instead"
        )

    product_gid = product_gid_for_variant_id(variant_ids[0]) if variant_ids else None
    if product_gid:
        shopify_delete_product(product_gid)

    for variant_id in variant_ids:
        _forget_variant(session, variant_id)
    session.delete(product)
    session.flush()
    return {"product_id": product_id, "shopify_id": product_gid, "deleted": True}


def delete_variant(session: Session, variant_id: str) -> dict:
    """Remove one size/colourway, from Shopify and from wanas.db.

    Refused for the last variant of a product: Shopify has no such thing as a
    product with no variants, and a local row with none is a product the bot
    would offer and never be able to sell. Deleting the product is the honest
    way to say that.
    """
    variant = session.get(Variant, variant_id)
    if variant is None:
        return {"error": "variant_not_found"}

    product = session.get(Product, variant.product_id)
    if product is not None and len(product.variants) <= 1:
        raise ProductInUse("this is the product's only size; delete the product instead")
    if _sold_variant_ids(session, [variant_id]):
        raise ProductInUse("this size has been ordered before; archive the product instead")

    product_gid = product_gid_for_variant_id(variant_id)
    if product_gid:
        live = shopify_catalog.fetch_skus([variant_id]).get(variant_id)
        if live is not None:
            shopify_delete_variants(product_gid, [live.shopify_id])

    _forget_variant(session, variant_id)
    session.delete(variant)
    session.flush()

    if product is not None:
        # The flush removed the row; the loaded collection still holds it.
        # Summarising without this re-read leaves the product advertising the
        # size that just went.
        session.expire(product, ["variants"])
        _resummarise(product)
    session.flush()
    return {"variant_id": variant_id, "product_id": variant.product_id, "deleted": True}


def _resummarise(product: Product) -> None:
    """The product's own size/colour lists, after one of them went away."""
    product.sizes = sorted({v.size for v in product.variants if v.size})
    product.colors = sorted({v.color for v in product.variants if v.color})
    product.lengths = sorted({v.length for v in product.variants if v.length})
    product.color_images = {
        c: g for c, g in (product.color_images or {}).items() if c in set(product.colors)
    }


def archive_product(session: Session, product_id: str) -> dict:
    """Stop selling a product without destroying what it sold.

    Both halves of the statement: Shopify's `status: ARCHIVED` takes it off
    the storefront, `Product.archived` takes it out of the bot's search and
    out of `get_variants`, and `order_items` still reads.
    """
    product = session.get(Product, product_id)
    if product is None:
        return {"error": "product_not_found"}

    any_variant = next((v.variant_id for v in product.variants), None)
    product_gid = product_gid_for_variant_id(any_variant) if any_variant else None
    if product_gid:
        shopify_update_product_fields(product_gid, {"status": "ARCHIVED"})

    product.archived = True
    session.flush()
    return {"product_id": product_id, "shopify_id": product_gid, "archived": True}


def update_product(
    session: Session,
    product_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    category: str | None = None,
    department: str | None = None,
    style: list[str] | None = None,
    collection: str | None = None,
    size_chart: str | None = None,
    variant_updates: list[dict] | None = None,
    variant_images: list[dict] | None = None,
) -> dict:
    """Edit an existing product's Shopify-owned fields and/or wanas.db-owned
    fields. `variant_updates` is `[{"variant_id", "price"?, "original_price"?,
    "stock_qty"?}]` -- existing variants only; see the module docstring for
    why adding or removing one is out of scope here.

    `variant_images` is `[{"variant_id", "source"}]`, a new picture for the
    row a staff member picked it on. It lands on **every variant of that
    colourway**, not only the one row: a colour is what a photo is of, and
    leaving M/Olive on the old picture while S/Olive has the new one gives
    `catalog._overlay_images` two photos for one colour and the customer
    whichever came first.
    """
    product = session.get(Product, product_id)
    if product is None:
        return {"error": "product_not_found"}

    any_variant_id = next((v.variant_id for v in product.variants), None)
    product_gid = product_gid_for_variant_id(any_variant_id) if any_variant_id else None

    if product_gid and (title is not None or description is not None or category is not None):
        fields = {}
        if title is not None:
            fields["title"] = title
        if description is not None:
            fields["descriptionHtml"] = description
        if category is not None:
            fields["productType"] = category
        shopify_update_product_fields(product_gid, fields)

    if product_gid and variant_updates:
        # Each variant's own gid, keyed the same way everything else in this
        # codebase looks one up: by SKU, via the same call
        # `shopify_catalog.fetch_skus` already makes for the live chat path.
        wanted_ids = [vu["variant_id"] for vu in variant_updates]
        live_by_id = shopify_catalog.fetch_skus(wanted_ids)

        bulk = []
        quantities = []
        for vu in variant_updates:
            variant = session.get(Variant, vu["variant_id"])
            live = live_by_id.get(vu["variant_id"])
            if variant is None or live is None:
                continue
            entry = {"id": live.shopify_id}
            if vu.get("price") is not None:
                entry["price"] = f"{Decimal(str(vu['price'])):.2f}"
            if vu.get("original_price") is not None:
                entry["compareAtPrice"] = f"{Decimal(str(vu['original_price'])):.2f}"
            if len(entry) > 1:
                bulk.append(entry)
            if vu.get("stock_qty") is not None:
                item_id = live.inventory_item_id
                quantities.append({"inventory_item_id": item_id, "quantity": int(vu["stock_qty"])})

        if bulk:
            shopify_update_variants(product_gid, bulk)
        shopify_set_inventory(quantities)

    if product_gid and variant_images:
        _replace_variant_images(session, product, product_gid, variant_images)

    # wanas.db side: always applied, whether or not Shopify was reachable for
    # the fields above -- `category`/`department`/`style`/`collection`/
    # `size_chart` have no Shopify home to fail on.
    if title is not None:
        product.name = title
    if description is not None:
        product.description = description
    if category is not None:
        product.category = category
    if department is not None:
        product.department = department
    if style is not None:
        product.style = style
    if collection is not None:
        product.collection = collection
    if size_chart is not None:
        product.size_chart = size_chart

    for vu in variant_updates or []:
        variant = session.get(Variant, vu["variant_id"])
        if variant is None:
            continue
        if "price" in vu and vu["price"] is not None:
            variant.price = Decimal(str(vu["price"]))
        if "original_price" in vu and vu["original_price"] is not None:
            variant.original_price = Decimal(str(vu["original_price"]))
            variant.on_sale = variant.original_price > variant.price
        if "stock_qty" in vu and vu["stock_qty"] is not None:
            variant.stock_qty = int(vu["stock_qty"])

    session.flush()
    return {"product_id": product_id, "shopify_id": product_gid}


__all__ = [
    "ProductInUse",
    "ProductRejected",
    "ShopifyConfigError",
    "ShopifyUnavailable",
    "list_products",
    "get_product",
    "create_product",
    "shopify_attach_images",
    "shopify_assign_variant_media",
    "update_product",
    "delete_product",
    "delete_variant",
    "archive_product",
    "product_gid_for_variant_id",
    "product_types",
    "slugify",
]
