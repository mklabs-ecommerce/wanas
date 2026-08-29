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

Removing a variant from an existing Shopify product is deliberately not
supported here -- it is destructive to order history on Shopify's side in a
way this module has no story for yet. Do that in Shopify Admin directly.
"""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal

from sqlalchemy.orm import Session

from domain.models import Product, Variant
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

PRODUCT_DETAIL_BY_SKU = """
query($query: String!) {
  productVariants(first: 1, query: $query) {
    nodes { product { id } }
  }
}
"""

PRODUCT_CREATE = """
mutation($input: ProductInput!) {
  productCreate(input: $input) {
    product { id title }
    userErrors { field message }
  }
}
"""

VARIANTS_CREATE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(productId: $productId, variants: $variants) {
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
    options = [{"name": "Size", "values": sorted({v["size"] for v in variants})}]
    colors = sorted({v["color"] for v in variants if v.get("color")})
    if colors:
        options.append({"name": "Color", "values": colors})
    lengths = sorted({v["length"] for v in variants if v.get("length")})
    if lengths:
        options.append({"name": "Length", "values": lengths})
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


def shopify_create_product(*, title: str, description: str, category: str) -> str:
    """Returns the new product's Shopify id."""
    client = get_admin_client()
    created = client(
        PRODUCT_CREATE,
        {
            "input": {
                "title": title,
                "descriptionHtml": description or "",
                "productType": category,
                "status": "ACTIVE",
            }
        },
    )
    _errors(created.get("productCreate"), "productCreate")
    return (created["productCreate"]["product"] or {})["id"]


def shopify_set_product_options(product_gid: str, options: list[dict]) -> None:
    """The default variant Shopify creates a product with has to be described
    by the same options every real variant uses, or `shopify_create_variants`
    rejects them as belonging to a different option set -- so this runs
    before that, not after."""
    client = get_admin_client()
    client(PRODUCT_UPDATE, {"input": {"id": product_gid, "productOptions": options}})


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
    """
    if not variants:
        raise ProductRejected("a product needs at least one variant")

    product_id = _unique_product_id(session, title)

    product_gid = shopify_create_product(title=title, description=description, category=category)
    shopify_set_product_options(product_gid, _build_options(variants))

    bulk_input = [
        {
            "optionValues": _option_values(v),
            "price": f"{Decimal(str(v['price'])):.2f}",
            "compareAtPrice": (
                f"{Decimal(str(v['original_price'])):.2f}"
                if v.get("original_price") and Decimal(str(v["original_price"])) > Decimal(str(v["price"]))
                else None
            ),
            "sku": _variant_id(product_id, v["size"], v.get("color"), v.get("length")),
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

    return {"product_id": product_id, "shopify_id": product_gid}


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
) -> dict:
    """Edit an existing product's Shopify-owned fields and/or wanas.db-owned
    fields. `variant_updates` is `[{"variant_id", "price"?, "original_price"?,
    "stock_qty"?}]` -- existing variants only; see the module docstring for
    why adding or removing one is out of scope here.
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
    "ProductRejected",
    "ShopifyConfigError",
    "ShopifyUnavailable",
    "list_products",
    "get_product",
    "create_product",
    "shopify_attach_images",
    "shopify_assign_variant_media",
    "update_product",
    "product_gid_for_variant_id",
    "slugify",
]
