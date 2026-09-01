"""Reading the size-chart metafields back out of Shopify.

`size_charts.py` publishes: `data/size_charts.json` -> `custom.size_chart`
(the diagram, a file reference) and `custom.size_chart_data` (the
measurements, as JSON the theme renders). That was one direction only, which
was fine while the file was the only place a chart was ever authored.

It is not any more. A chart edited in Shopify Admin -- a corrected
measurement, a diagram swapped for a better one, a chart added to a product
created straight in Shopify -- lived only on the storefront. The bot reads
`data/size_charts.json` and the `size_charts` table, so it kept quoting the
old numbers, or said there was no chart while the product page showed one.

This is the other direction, and it follows the same rules as
`product_import.py`: additive, idempotent, and never deleting. A product
whose metafields are empty is left exactly as it is -- absence in Shopify is
not a statement that the local chart is wrong, and the twelve shipped charts
have no metafield origin to lose.

Two rules keep a round trip from being mistaken for news:

* **A chart Shopify agrees with is skipped.** What comes back is compared
  against the merged local view (`domain/services/size_charts.get_chart`). If
  they say the same thing there is nothing to learn, and writing a row anyway
  would take `data/size_charts.json` out of play for that id -- replacing a
  file that ships with the code with a CDN url that can 404.
* **A chart with no id of ours is not given one of ours.** Our publisher
  always writes `chart_id` into the JSON, so a payload without one was
  authored in Admin. It lands under `shopify-<product_id>` rather than
  claiming an existing id, because a chart three products share must not be
  silently rewritten by an edit somebody made on one of them.

Matching a Shopify product to a local one is by variant SKU, the same way
`catalog.py`, `product_import.py` and `size_charts.py` do it.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from domain.models import Product, SizeChart, Variant
from domain.services import size_charts as local_charts
from integrations.shopify.client import get_admin_client
from integrations.shopify.size_charts import DATA_KEY, IMAGE_KEY, NAMESPACE

log = logging.getLogger("wanas.shopify.size_chart_import")

#: How a chart that came from Shopify Admin is named locally. Prefixed on
#: purpose: it can never collide with a `data/size_charts.json` id, so
#: importing one product's hand-made chart cannot hijack the chart its two
#: siblings share.
MINTED_PREFIX = "shopify-"

#: `SizeChart.source` for a row this module wrote. A third value beside
#: "manual" (typed by a staff member) and "vision" (read off a picture and
#: confirmed by one): nobody here checked these numbers, Shopify is simply
#: where they were.
SOURCE = "shopify"

PRODUCT_CHARTS = """
query($cursor: String, $namespace: String!, $imageKey: String!, $dataKey: String!) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      chartImage: metafield(namespace: $namespace, key: $imageKey) {
        reference {
          ... on MediaImage { id image { url } }
          ... on GenericFile { id url }
        }
      }
      chartData: metafield(namespace: $namespace, key: $dataKey) { value }
      variants(first: 100) { nodes { sku } }
    }
  }
}
"""


class EmptyRead(RuntimeError):
    """Shopify answered with no products at all.

    The same guard `product_reconcile` carries: an empty live read is far more
    likely to be a token, a scope or an outage than a shop that sells nothing,
    and acting on it writes nonsense over good data.
    """


def _image_of(metafield: dict | None) -> tuple[str | None, str | None]:
    """`(url, file_gid)` behind a `file_reference` metafield.

    Two shapes, because Shopify Files stores a picture as a `MediaImage` and
    anything else as a `GenericFile`, and a staff member can attach either.
    """
    reference = (metafield or {}).get("reference") or {}
    url = (reference.get("image") or {}).get("url") or reference.get("url")
    return (url or None), (reference.get("id") or None)


def iter_shopify_charts() -> list[dict]:
    """Every Shopify product, with whatever chart metafields it carries."""
    client = get_admin_client()
    out: list[dict] = []
    cursor = None
    while True:
        data = client(
            PRODUCT_CHARTS,
            {
                "cursor": cursor,
                "namespace": NAMESPACE,
                "imageKey": IMAGE_KEY,
                "dataKey": DATA_KEY,
            },
        )
        block = data.get("products") or {}
        for node in block.get("nodes") or []:
            url, file_gid = _image_of(node.get("chartImage"))
            skus = [
                (v.get("sku") or "").strip()
                for v in (node.get("variants") or {}).get("nodes") or []
            ]
            out.append(
                {
                    "gid": node.get("id"),
                    "title": node.get("title"),
                    "skus": [sku for sku in skus if sku],
                    "data": (node.get("chartData") or {}).get("value"),
                    "image_url": url,
                    "image_file_gid": file_gid,
                }
            )
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if cursor is None:
            break
    if not out:
        raise EmptyRead("Shopify returned no products; refusing to act on an empty read")
    return out


def chart_from_payload(payload: dict) -> dict | None:
    """`storefront_payload` inverted: the theme's shape back into the file's.

    Sizes went out as an ordered array precisely because Liquid cannot sort an
    object's keys back into S / M / L. They come back as an object built in
    that array's order, so the order survives the round trip.

    None when there is nothing a bot could quote -- no columns, or no sizes.
    A chart like that is a picture, and the caller stores it as one.
    """
    if not isinstance(payload, dict):
        return None
    measurements = [
        m for m in (payload.get("measurements") or []) if isinstance(m, dict) and m.get("key")
    ]
    sizes: dict[str, dict] = {}
    for entry in payload.get("sizes") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        values = entry.get("values")
        if not name or not isinstance(values, dict):
            continue
        # Only the columns the chart declares, and only cells with something
        # in them: a blank cell stays blank rather than becoming a confident
        # 0, the same rule `normalise_chart_reading` applies to a vision
        # reading of the same kind of grid.
        kept = {
            m["key"]: values[m["key"]]
            for m in measurements
            if values.get(m["key"]) is not None
        }
        if kept:
            sizes[str(name)] = kept
    if not measurements or not sizes:
        return None
    return {
        "chart_id": payload.get("chart_id") or None,
        "title": payload.get("title") or "",
        "unit": payload.get("unit") or "cm",
        "measurements": measurements,
        "sizes": sizes,
    }


def _same_chart(incoming: dict, existing: dict | None) -> bool:
    """Whether what Shopify sent says what we already say.

    Compared on what a customer is actually told -- the columns, the numbers
    and the unit -- and not on the picture or the title, so a round trip of
    our own publish is recognised as one while a real edit is not.
    """
    if existing is None:
        return False
    return (
        (incoming.get("unit") or "cm") == (existing.get("unit") or "cm")
        and [m.get("key") for m in incoming["measurements"]]
        == [m.get("key") for m in existing.get("measurements") or []]
        and incoming["sizes"] == (existing.get("sizes") or {})
    )


def _local_by_sku(session: Session) -> dict[str, str]:
    rows = session.query(Variant.variant_id, Variant.product_id).all()
    return {row.variant_id: row.product_id for row in rows}


def build_plan(session: Session, nodes: list[dict]) -> dict:
    """What would be written, per product, without writing any of it."""
    by_sku = _local_by_sku(session)
    charts: dict[str, dict] = {}
    products: list[dict] = []
    unmatched: list[dict] = []
    unchanged: list[str] = []

    for node in nodes:
        if not (node.get("data") or node.get("image_url")):
            continue  # nothing published on this product, which is not news
        product_id = next((by_sku[sku] for sku in node["skus"] if sku in by_sku), None)
        if product_id is None:
            unmatched.append({"gid": node["gid"], "title": node["title"]})
            continue
        product = session.get(Product, product_id)
        if product is None:  # pragma: no cover - a sku with no product row
            continue

        incoming = None
        if node.get("data"):
            try:
                incoming = chart_from_payload(json.loads(node["data"]))
            except (ValueError, TypeError):
                log.warning("%s: custom.%s is not valid JSON", product_id, DATA_KEY)

        entry = {
            "product_id": product_id,
            "name": product.name,
            "image_url": node.get("image_url"),
            "image_file_gid": node.get("image_file_gid"),
            "chart_id": None,
            "chart": None,
            "sets_product_image": False,
        }

        if incoming is not None:
            chart_id = incoming["chart_id"] or f"{MINTED_PREFIX}{product_id}"
            if _same_chart(incoming, local_charts.get_chart(chart_id, session)):
                unchanged.append(product_id)
            else:
                entry["chart_id"] = chart_id
                entry["chart"] = incoming
                charts[chart_id] = incoming

        # The picture belongs to the chart when there is one and to the
        # product when there is not -- exactly the two homes
        # `catalog_tools._chart_image` reads, in that order.
        if entry["image_url"] and entry["chart"] is None and not product.size_chart:
            entry["sets_product_image"] = product.size_chart_image != entry["image_url"]

        if entry["chart"] or entry["sets_product_image"]:
            products.append(entry)

    return {
        "products": products,
        "charts": charts,
        "unmatched": unmatched,
        "unchanged": sorted(set(unchanged)),
    }


def apply_plan(session: Session, plan: dict) -> dict:
    """Write the plan. Additive: nothing here clears a column or drops a row."""
    written_charts = 0
    linked = 0
    images = 0

    for entry in plan["products"]:
        product = session.get(Product, entry["product_id"])
        if product is None:  # pragma: no cover - the plan was built from these
            continue

        if entry["chart"]:
            chart_id = entry["chart_id"]
            row = session.get(SizeChart, chart_id)
            if row is None:
                row = SizeChart(chart_id=chart_id)
                session.add(row)
            row.title = entry["chart"]["title"] or product.name
            row.unit = entry["chart"]["unit"]
            row.measurements = entry["chart"]["measurements"]
            row.sizes = entry["chart"]["sizes"]
            row.source = SOURCE
            if entry["image_url"]:
                row.image_url = entry["image_url"]
                row.image_file_gid = entry["image_file_gid"]
            written_charts += 1
            if product.size_chart != chart_id:
                product.size_chart = chart_id
                linked += 1

        if entry["sets_product_image"]:
            product.size_chart_image = entry["image_url"]
            images += 1

    session.flush()
    return {"charts": written_charts, "linked": linked, "product_images": images}


__all__ = [
    "EmptyRead",
    "MINTED_PREFIX",
    "SOURCE",
    "apply_plan",
    "build_plan",
    "chart_from_payload",
    "iter_shopify_charts",
]
