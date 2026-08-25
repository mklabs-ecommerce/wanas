"""Store-wide inventory, flattened to one row per variant.

`admin_products.py` answers "what is this product"; this answers "what is
about to run out", which is a different question with a different shape: the
unit is the *variant*, not the product, and the interesting rows are the ones
nobody would find by browsing the product list.

Stock is still written through `admin_products.shopify_set_inventory` -- the
one place that knows the location id and the correction semantics. This
module only adds the read, plus the SKU->inventory-item mapping a quick edit
needs so the caller does not have to open the full product first.
"""

from __future__ import annotations

from integrations.shopify.client import (  # noqa: F401  (re-exported)
    ShopifyConfigError,
    ShopifyUnavailable,
    get_admin_client,
)

#: Rows at or below this are "low" unless the caller says otherwise. Chosen
#: to match how this shop restocks (small runs, per size/colour), not as a
#: universal number.
DEFAULT_LOW_STOCK = 3

#: Hard ceiling on pages walked for one request, the same guard
#: `domain/services/dashboard_stats.py` puts on its own pagination: at 25
#: products a page this is 2,500 products, past which the page still returns.
MAX_PAGES = 100

INVENTORY_QUERY = """
query($cursor: String, $query: String) {
  products(first: 25, after: $cursor, query: $query) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      status
      productType
      featuredImage { url }
      variants(first: 100) {
        nodes {
          id
          sku
          price
          inventoryQuantity
          inventoryItem { id }
          image { url }
          selectedOptions { name value }
        }
      }
    }
  }
}
"""


def _option(variant: dict, name: str) -> str | None:
    for option in variant.get("selectedOptions") or []:
        if (option.get("name") or "").lower() == name.lower():
            return option.get("value")
    return None


def _rows_for_product(node: dict) -> list[dict]:
    rows = []
    for variant in (node.get("variants") or {}).get("nodes") or []:
        rows.append(
            {
                "product_id": node["id"],
                "product_title": node.get("title") or "",
                "product_status": node.get("status"),
                "category": node.get("productType"),
                # The variant's own photo first. This table has one row per
                # colourway, and the product's featured image put the same
                # black tee next to every one of them -- a row labelled Navy
                # showing the black photo is the table getting it wrong, not
                # a missing nicety.
                "image_url": (variant.get("image") or {}).get("url")
                or (node.get("featuredImage") or {}).get("url"),
                "variant_id": variant["id"],
                "sku": variant.get("sku") or "",
                "price": variant.get("price") or "0.00",
                "quantity": int(variant.get("inventoryQuantity") or 0),
                "inventory_item_id": (variant.get("inventoryItem") or {}).get("id"),
                "size": _option(variant, "Size"),
                "color": _option(variant, "Color"),
                "length": _option(variant, "Length"),
            }
        )
    return rows


def inventory_rows(*, query: str | None = None, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    """Every variant in the store as a flat row. Returns `(rows, truncated)`;
    `truncated` is True only when `max_pages` was hit, so the caller can say
    the totals are a floor rather than quietly under-reporting."""
    client = get_admin_client()
    rows: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        data = client(INVENTORY_QUERY, {"cursor": cursor, "query": query})
        block = data.get("products") or {}
        for node in block.get("nodes") or []:
            rows.extend(_rows_for_product(node))
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return rows, False
        cursor = page.get("endCursor")
    return rows, True


def summarize(rows: list[dict], *, low_stock_at: int = DEFAULT_LOW_STOCK) -> dict:
    """Counts and stock value for the header tiles. Stock value is at the
    *selling* price -- this shop has no cost-per-item in Shopify, so calling
    it inventory cost would be a number that is simply wrong."""
    out_of_stock = [r for r in rows if r["quantity"] <= 0]
    low = [r for r in rows if 0 < r["quantity"] <= low_stock_at]
    units = sum(max(r["quantity"], 0) for r in rows)
    value = sum(max(r["quantity"], 0) * float(r["price"] or 0) for r in rows)
    return {
        "variant_count": len(rows),
        "product_count": len({r["product_id"] for r in rows}),
        "out_of_stock_count": len(out_of_stock),
        "low_stock_count": len(low),
        "total_units": units,
        "retail_value": f"{value:.2f}",
        "low_stock_at": low_stock_at,
    }


__all__ = [
    "DEFAULT_LOW_STOCK",
    "ShopifyConfigError",
    "ShopifyUnavailable",
    "inventory_rows",
    "summarize",
]
