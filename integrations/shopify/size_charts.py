"""Publishing the size charts to Shopify, so the storefront can show them.

The bot has always had these: `data/size_charts.json` holds the measurements
and `data/size-charts/*.png` the diagrams, and `Product.size_chart` says which
chart a product uses. None of that is visible to a customer browsing the
Shopify storefront, who is the one person most likely to want it before
choosing a size.

Two metafields carry it across, both under the `custom` namespace Shopify
reserves for the merchant:

- `custom.size_chart` -- a **file reference** to the diagram, uploaded to
  Shopify Files once and reused by every product on that chart.
- `custom.size_chart_data` -- the measurements as **JSON**, so the theme can
  render a real table in both languages rather than embedding text in a
  picture. `data/size_charts.json` already carries `label_en` and `label_ar`
  for every row, which is the whole reason this is worth doing as data.

The JSON is reshaped on the way out, not copied: sizes go from an object to an
ordered array. Liquid iterates an object in whatever order it arrives in, and
a size chart that lists XL before S is worse than no size chart.

Matching a local product to its Shopify product is by variant SKU, the same
way `catalog.py` and `product_import.py` do it -- `Product.product_id` means
nothing to Shopify.

Everything here is additive and idempotent: a file already uploaded is reused,
a definition that already exists is left alone, and a metafield is overwritten
with the same value rather than duplicated. Nothing is ever deleted.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path

from sqlalchemy.orm import Session

from domain.models import Product, Variant
from integrations.shopify import files as shopify_files
from integrations.shopify.client import ShopifyUnavailable, get_admin_client

log = logging.getLogger("wanas.shopify.size_charts")

ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS_JSON = ROOT / "data" / "size_charts.json"

NAMESPACE = "custom"
#: The diagram, as a file reference.
IMAGE_KEY = "size_chart"
#: The measurements, as JSON the theme renders bilingually.
DATA_KEY = "size_chart_data"

_ACCESS_DENIED = "ACCESS_DENIED"

# --------------------------------------------------------------------------
# GraphQL
# --------------------------------------------------------------------------

DEFINITION_CREATE = """
mutation($definition: MetafieldDefinitionInput!) {
  metafieldDefinitionCreate(definition: $definition) {
    createdDefinition { id key }
    userErrors { field message code }
  }
}
"""

DEFINITIONS_QUERY = """
query($namespace: String!) {
  metafieldDefinitions(first: 50, ownerType: PRODUCT, namespace: $namespace) {
    nodes { key }
  }
}
"""

FILES_QUERY = """
query($query: String!) {
  files(first: 10, query: $query) {
    nodes {
      id
      ... on MediaImage { image { url } }
    }
  }
}
"""

PRODUCT_SKUS = """
query($cursor: String, $namespace: String!, $key: String!) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      metafield(namespace: $namespace, key: $key) { id }
      variants(first: 100) { nodes { sku } }
    }
  }
}
"""

METAFIELDS_SET = """
mutation($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id key }
    userErrors { field message code }
  }
}
"""

#: Shopify's own cap on one `metafieldsSet` call.
SET_CHUNK = 25


class SizeChartError(RuntimeError):
    """Shopify refused something this module cannot work around."""


# --------------------------------------------------------------------------
# the local side
# --------------------------------------------------------------------------


def load_charts(path: Path | None = None) -> dict[str, dict]:
    return json.loads((path or CHARTS_JSON).read_text(encoding="utf-8"))


def chart_image(chart: dict) -> Path | None:
    """The diagram on disk, or None if the chart has no picture.

    A chart whose file is missing is not an error: the measurements are the
    part customers actually read, and half a chart beats none.
    """
    relative = chart.get("image")
    if not relative:
        return None
    path = ROOT / relative
    return path if path.exists() else None


def storefront_payload(chart: dict) -> dict:
    """The chart, shaped for a theme rather than for the bot.

    Sizes become an ordered array. Liquid has no way to sort the keys of a
    JSON object back into S / M / L / XL, so the order is decided here, once,
    where the source file's own order is still visible.
    """
    measurements = [
        {
            "key": m.get("key"),
            "label_en": m.get("label_en"),
            "label_ar": m.get("label_ar"),
            "marker": m.get("marker"),
        }
        for m in chart.get("measurements") or []
    ]
    sizes = [
        {"name": name, "values": {m["key"]: values.get(m["key"]) for m in measurements}}
        for name, values in (chart.get("sizes") or {}).items()
    ]
    return {
        "chart_id": chart.get("chart_id"),
        "title": chart.get("title"),
        "unit": chart.get("unit") or "cm",
        "measurements": measurements,
        "sizes": sizes,
    }


def products_by_chart(session: Session) -> dict[str, list[dict]]:
    """`{chart_id: [{product_id, name, skus}]}` for every product that names a
    chart. A product with no `size_chart` is skipped, not guessed at."""
    out: dict[str, list[dict]] = {}
    rows = session.query(Product).filter(Product.size_chart.isnot(None)).all()
    for product in rows:
        chart_id = (product.size_chart or "").strip()
        if not chart_id:
            continue
        skus = [
            v.variant_id
            for v in session.query(Variant).filter(Variant.product_id == product.product_id).all()
        ]
        out.setdefault(chart_id, []).append(
            {"product_id": product.product_id, "name": product.name, "skus": skus}
        )
    return out


# --------------------------------------------------------------------------
# the Shopify side
# --------------------------------------------------------------------------


def existing_definitions() -> set[str]:
    """Which of ours Shopify already defines."""
    client = get_admin_client()
    data = client(DEFINITIONS_QUERY, {"namespace": NAMESPACE})
    nodes = (data.get("metafieldDefinitions") or {}).get("nodes") or []
    return {n["key"] for n in nodes} & {IMAGE_KEY, DATA_KEY}


def ensure_definitions(*, apply: bool) -> list[str]:
    """Create the two metafield definitions if they are not there yet.

    A definition is what makes the field appear in Shopify Admin and gives it
    a type; the values below would work without one, but staff would have no
    way to see or fix them by hand.
    """
    wanted = [
        {
            "name": "Size chart",
            "namespace": NAMESPACE,
            "key": IMAGE_KEY,
            "description": "The size-chart diagram for this product.",
            "type": "file_reference",
            "ownerType": "PRODUCT",
        },
        {
            "name": "Size chart data",
            "namespace": NAMESPACE,
            "key": DATA_KEY,
            "description": "Measurements the theme renders as a bilingual table.",
            "type": "json",
            "ownerType": "PRODUCT",
        },
    ]
    if not apply:
        have = existing_definitions()
        return [d["key"] for d in wanted if d["key"] not in have]

    client = get_admin_client()
    created = []
    for definition in wanted:
        result = client(DEFINITION_CREATE, {"definition": definition})
        block = result.get("metafieldDefinitionCreate") or {}
        errors = block.get("userErrors") or []
        taken = any((e.get("code") or "").upper() == "TAKEN" for e in errors)
        if errors and not taken:
            raise SizeChartError("; ".join(e.get("message", "") for e in errors))
        if not taken:
            created.append(definition["key"])
    return created


def _existing_file(client, filename: str) -> str | None:
    data = client(FILES_QUERY, {"query": f"filename:{filename}"})
    nodes = (data.get("files") or {}).get("nodes") or []
    return nodes[0]["id"] if nodes else None


def upload_image(client, path: Path) -> str:
    """Put one diagram in Shopify Files and return its gid.

    The staged-upload dance itself lives in `files.py`, shared with the
    dashboard's product-photo upload -- there is one right way to hand
    Shopify bytes and it should not be written twice.
    """
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    try:
        return shopify_files.upload_to_files(
            client, path.name, path.read_bytes(), mime, alt=f"{path.stem} size chart"
        )["id"]
    except shopify_files.FileUploadError as exc:
        raise SizeChartError(str(exc)) from exc


def ensure_files(charts: dict[str, dict], *, apply: bool) -> dict[str, str]:
    """`{chart_id: file_gid}`, uploading only what Shopify does not have."""
    client = get_admin_client()
    out: dict[str, str] = {}
    for chart_id, chart in charts.items():
        path = chart_image(chart)
        if path is None:
            log.warning("%s has no diagram on disk; its table will go up alone", chart_id)
            continue
        try:
            existing = _existing_file(client, path.name)
        except ShopifyUnavailable as exc:
            if _ACCESS_DENIED in str(exc):
                raise SizeChartError(
                    "the app has no access to Files; add the read_files and "
                    "write_files scopes in Shopify"
                ) from exc
            raise
        if existing:
            out[chart_id] = existing
            continue
        if not apply:
            continue
        out[chart_id] = upload_image(client, path)
        log.info("uploaded %s", path.name)
    return out


def product_gids_by_sku() -> tuple[dict[str, str], set[str]]:
    """`({sku: product_gid}, {gid that already has a chart image set})`.

    The second half is what keeps a chart somebody uploaded by hand in Shopify
    Admin from being replaced by ours: theirs was a decision, ours is a
    default.
    """
    client = get_admin_client()
    out: dict[str, str] = {}
    has_image: set[str] = set()
    cursor = None
    while True:
        data = client(
            PRODUCT_SKUS, {"cursor": cursor, "namespace": NAMESPACE, "key": IMAGE_KEY}
        )
        block = data.get("products") or {}
        for node in block.get("nodes") or []:
            if node.get("metafield"):
                has_image.add(node["id"])
            for variant in (node.get("variants") or {}).get("nodes") or []:
                sku = (variant.get("sku") or "").strip()
                if sku:
                    out[sku] = node["id"]
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return out, has_image
        cursor = page.get("endCursor")
        if cursor is None:
            return out, has_image


def build_plan(
    session: Session,
    charts: dict[str, dict],
    files: dict[str, str],
    *,
    replace_images: bool = False,
) -> dict:
    """What would be written, per product, without writing any of it."""
    by_sku, has_image = product_gids_by_sku()
    entries: list[dict] = []
    unmatched: list[dict] = []
    unknown_charts: list[str] = []

    for chart_id, products in sorted(products_by_chart(session).items()):
        chart = charts.get(chart_id)
        if chart is None:
            unknown_charts.append(chart_id)
            continue
        for product in products:
            gid = next((by_sku[sku] for sku in product["skus"] if sku in by_sku), None)
            if gid is None:
                unmatched.append({**product, "chart_id": chart_id})
                continue
            keep_theirs = gid in has_image and not replace_images
            entries.append(
                {
                    "product_id": product["product_id"],
                    "name": product["name"],
                    "gid": gid,
                    "chart_id": chart_id,
                    "file_gid": None if keep_theirs else files.get(chart_id),
                    "kept_existing_image": keep_theirs,
                    "data": storefront_payload(chart),
                }
            )
    return {"entries": entries, "unmatched": unmatched, "unknown_charts": unknown_charts}


def set_product_chart_image(product_gid: str, file_gid: str) -> None:
    """Point one product's `custom.size_chart` at an already-uploaded file.

    The bulk path (`write_plan`) writes the table and the diagram together
    from `size_charts.json`. This is the other door: a staff member creating a
    product in the dashboard uploads a chart picture and nothing else, so the
    diagram metafield is set on its own and the product simply has no table.
    """
    client = get_admin_client()
    result = client(
        METAFIELDS_SET,
        {
            "metafields": [
                {
                    "ownerId": product_gid,
                    "namespace": NAMESPACE,
                    "key": IMAGE_KEY,
                    "type": "file_reference",
                    "value": file_gid,
                }
            ]
        },
    )
    block = result.get("metafieldsSet") or {}
    errors = block.get("userErrors") or []
    if errors:
        raise SizeChartError("; ".join(e.get("message", "") for e in errors))


def set_product_chart(product_gid: str, chart: dict, file_gid: str | None = None) -> None:
    """Both metafields for one product, from one chart.

    `write_plan` does this in bulk for every product on every chart in
    `size_charts.json`. This is the single-product door the dashboard uses
    when a staff member makes a chart there: the same two keys, the same
    reshaping through `storefront_payload`, so a chart built in the dashboard
    renders on the storefront identically to one that shipped in the file.

    `file_gid` is optional -- a chart whose picture is already on the product
    (or which has no picture) writes only the table.
    """
    metafields = [
        {
            "ownerId": product_gid,
            "namespace": NAMESPACE,
            "key": DATA_KEY,
            "type": "json",
            "value": json.dumps(storefront_payload(chart), ensure_ascii=False),
        }
    ]
    if file_gid:
        metafields.append(
            {
                "ownerId": product_gid,
                "namespace": NAMESPACE,
                "key": IMAGE_KEY,
                "type": "file_reference",
                "value": file_gid,
            }
        )
    client = get_admin_client()
    result = client(METAFIELDS_SET, {"metafields": metafields})
    errors = (result.get("metafieldsSet") or {}).get("userErrors") or []
    if errors:
        raise SizeChartError("; ".join(e.get("message", "") for e in errors))


def write_plan(entries: list[dict]) -> int:
    """Set both metafields on every product in the plan."""
    if not entries:
        return 0
    client = get_admin_client()
    metafields = []
    for entry in entries:
        metafields.append(
            {
                "ownerId": entry["gid"],
                "namespace": NAMESPACE,
                "key": DATA_KEY,
                "type": "json",
                "value": json.dumps(entry["data"], ensure_ascii=False),
            }
        )
        if entry.get("file_gid"):
            metafields.append(
                {
                    "ownerId": entry["gid"],
                    "namespace": NAMESPACE,
                    "key": IMAGE_KEY,
                    "type": "file_reference",
                    "value": entry["file_gid"],
                }
            )

    written = 0
    for i in range(0, len(metafields), SET_CHUNK):
        chunk = metafields[i : i + SET_CHUNK]
        result = client(METAFIELDS_SET, {"metafields": chunk})
        block = result.get("metafieldsSet") or {}
        errors = block.get("userErrors") or []
        if errors:
            raise SizeChartError("; ".join(e.get("message", "") for e in errors))
        written += len(block.get("metafields") or [])
    return written


__all__ = [
    "NAMESPACE",
    "IMAGE_KEY",
    "DATA_KEY",
    "SizeChartError",
    "build_plan",
    "chart_image",
    "ensure_definitions",
    "ensure_files",
    "load_charts",
    "product_gids_by_sku",
    "products_by_chart",
    "storefront_payload",
    "write_plan",
]
