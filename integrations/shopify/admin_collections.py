"""Shopify collections, for the dashboard's Collections view.

The fourth Shopify-admin surface, alongside `admin_products.py`,
`admin_orders.py` and `admin_customers.py`, and shaped exactly like them: a
GraphQL query per operation, dicts out, `ShopifyUnavailable` /
`ShopifyConfigError` the only failure vocabulary the layer above has to know.

Scope is deliberately the same line `admin_products.py` draws. A collection
is a *Shopify* object with no wanas.db mirror -- the local `collection`
column on `Product` is a free-text merchandising label the bot searches on
(`domain/services/search_terms.py`), not a foreign key -- so nothing here
writes to Postgres. Only manual collections can have products added or
removed; a smart (rule-driven) collection is reported as such and its
membership is left to its rules, because editing it by hand here would be
silently undone by Shopify the next time the rules re-evaluate.
"""

from __future__ import annotations

from integrations.shopify.client import (  # noqa: F401  (re-exported)
    ShopifyConfigError,
    ShopifyUnavailable,
    get_admin_client,
)


class CollectionRejected(RuntimeError):
    """Shopify accepted the call and refused the change -- a duplicate
    handle, a smart collection asked to take a manual member. Distinct from
    `ShopifyUnavailable`, which means we never got an answer at all."""


COLLECTIONS_QUERY = """
query($cursor: String, $query: String) {
  collections(first: 50, after: $cursor, query: $query, sortKey: UPDATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      handle
      updatedAt
      ruleSet { appliedDisjunctively rules { column relation condition } }
      image { url }
      productsCount { count }
    }
  }
}
"""

COLLECTION_DETAIL_QUERY = """
query($id: ID!) {
  collection(id: $id) {
    id
    title
    handle
    descriptionHtml
    updatedAt
    ruleSet { appliedDisjunctively rules { column relation condition } }
    image { url }
    productsCount { count }
    products(first: 100) {
      nodes {
        id
        title
        status
        featuredImage { url }
        totalInventory
      }
    }
  }
}
"""

COLLECTION_CREATE = """
mutation($input: CollectionInput!) {
  collectionCreate(input: $input) {
    collection { id title handle }
    userErrors { field message }
  }
}
"""

COLLECTION_UPDATE = """
mutation($input: CollectionInput!) {
  collectionUpdate(input: $input) {
    collection { id title handle }
    userErrors { field message }
  }
}
"""

COLLECTION_ADD_PRODUCTS = """
mutation($id: ID!, $productIds: [ID!]!) {
  collectionAddProducts(id: $id, productIds: $productIds) {
    collection { id }
    userErrors { field message }
  }
}
"""

COLLECTION_REMOVE_PRODUCTS = """
mutation($id: ID!, $productIds: [ID!]!) {
  collectionRemoveProducts(id: $id, productIds: $productIds) {
    job { id }
    userErrors { field message }
  }
}
"""


def _errors(block: dict | None, key: str) -> None:
    """Same contract as `admin_products._errors`: a userErrors list is a
    refusal, not an outage, and must not be retried as one."""
    errors = (block or {}).get("userErrors") or []
    if errors:
        detail = "; ".join(e.get("message", "?") for e in errors)
        raise CollectionRejected(f"{key}: {detail}")


def _summary(node: dict) -> dict:
    rule_set = node.get("ruleSet")
    return {
        "id": node["id"],
        "title": node.get("title") or "",
        "handle": node.get("handle") or "",
        "updated_at": node.get("updatedAt"),
        "image_url": (node.get("image") or {}).get("url"),
        "product_count": (node.get("productsCount") or {}).get("count") or 0,
        # A smart collection's membership is owned by its rules. The frontend
        # uses this to hide "add product", not merely to draw a badge.
        "smart": bool(rule_set),
        "rules": [
            {
                "column": rule.get("column"),
                "relation": rule.get("relation"),
                "condition": rule.get("condition"),
            }
            for rule in ((rule_set or {}).get("rules") or [])
        ],
        "rules_match_any": bool((rule_set or {}).get("appliedDisjunctively")),
    }


def list_collections(*, query: str | None = None, cursor: str | None = None) -> dict:
    client = get_admin_client()
    data = client(COLLECTIONS_QUERY, {"cursor": cursor, "query": query})
    block = data.get("collections") or {}
    page = block.get("pageInfo") or {}
    return {
        "collections": [_summary(n) for n in block.get("nodes") or []],
        "has_next_page": bool(page.get("hasNextPage")),
        "end_cursor": page.get("endCursor"),
    }


def get_collection(collection_gid: str) -> dict | None:
    client = get_admin_client()
    node = (client(COLLECTION_DETAIL_QUERY, {"id": collection_gid}) or {}).get("collection")
    if not node:
        return None
    detail = _summary(node)
    detail["description_html"] = node.get("descriptionHtml") or ""
    detail["products"] = [
        {
            "id": p["id"],
            "title": p.get("title"),
            "status": p.get("status"),
            "image_url": (p.get("featuredImage") or {}).get("url"),
            "inventory": p.get("totalInventory") or 0,
        }
        for p in ((node.get("products") or {}).get("nodes") or [])
    ]
    return detail


def create_collection(*, title: str, description_html: str = "") -> dict:
    client = get_admin_client()
    result = client(
        COLLECTION_CREATE,
        {"input": {"title": title, "descriptionHtml": description_html}},
    )
    _errors(result.get("collectionCreate"), "collectionCreate")
    created = (result.get("collectionCreate") or {}).get("collection") or {}
    return {"id": created.get("id"), "title": created.get("title"), "handle": created.get("handle")}


def update_collection(
    collection_gid: str, *, title: str | None = None, description_html: str | None = None
) -> dict:
    fields: dict = {"id": collection_gid}
    if title is not None:
        fields["title"] = title
    if description_html is not None:
        fields["descriptionHtml"] = description_html
    if len(fields) == 1:
        return {"id": collection_gid, "unchanged": True}

    client = get_admin_client()
    result = client(COLLECTION_UPDATE, {"input": fields})
    _errors(result.get("collectionUpdate"), "collectionUpdate")
    return {"id": collection_gid}


def add_products(collection_gid: str, product_gids: list[str]) -> dict:
    if not product_gids:
        return {"ok": True, "added": 0}
    client = get_admin_client()
    result = client(COLLECTION_ADD_PRODUCTS, {"id": collection_gid, "productIds": product_gids})
    _errors(result.get("collectionAddProducts"), "collectionAddProducts")
    return {"ok": True, "added": len(product_gids)}


def remove_products(collection_gid: str, product_gids: list[str]) -> dict:
    if not product_gids:
        return {"ok": True, "removed": 0}
    client = get_admin_client()
    result = client(COLLECTION_REMOVE_PRODUCTS, {"id": collection_gid, "productIds": product_gids})
    _errors(result.get("collectionRemoveProducts"), "collectionRemoveProducts")
    # Shopify removes asynchronously and hands back a job id; the caller
    # refetches rather than being told a count that is not true yet.
    return {"ok": True, "removed": len(product_gids), "async": True}


__all__ = [
    "CollectionRejected",
    "ShopifyConfigError",
    "ShopifyUnavailable",
    "list_collections",
    "get_collection",
    "create_collection",
    "update_collection",
    "add_products",
    "remove_products",
]
