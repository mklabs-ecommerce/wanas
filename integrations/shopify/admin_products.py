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
from decimal import Decimal

from sqlalchemy.orm import Session

from domain.models import Product, Variant
from integrations.shopify import catalog as shopify_catalog, inventory as shopify_inventory
from integrations.shopify.client import (
    ShopifyConfigError,
    ShopifyUnavailable,
    get_admin_client,
)

log = logging.getLogger("wanas.shopify.admin_products")

PAGE_SIZE = 25

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
    media { id }
    mediaUserErrors { field message }
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
    return [{"sku": n["sku"], "inventory_item_id": n["inventoryItem"]["id"]} for n in nodes]


def shopify_attach_media(product_gid: str, image_url: str) -> None:
    client = get_admin_client()
    result = client(
        MEDIA_CREATE,
        {"productId": product_gid, "media": [{"originalSource": image_url, "mediaContentType": "IMAGE"}]},
    )
    _errors(result.get("productCreateMedia"), "productCreateMedia")


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
) -> dict:
    """Create on Shopify, then mirror into wanas.db.

    `variants` is `[{"size", "color"?, "length"?, "price", "original_price"?,
    "stock_qty"}]`. Raises `ProductRejected` for a Shopify-side refusal (a bad
    field, a duplicate SKU) and `ShopifyUnavailable`/`ShopifyConfigError` for
    an outage -- the dashboard route tells those apart.
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

    if image_url:
        shopify_attach_media(product_gid, image_url)

    by_sku = {c["sku"]: c for c in created_variants}
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
    product.images = [image_url] if image_url else list(product.images or [])
    product.color_images = product.color_images or {}
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
    "update_product",
    "product_gid_for_variant_id",
    "slugify",
]
