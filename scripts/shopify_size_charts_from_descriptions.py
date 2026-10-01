"""Turn the size table written inside a Shopify product's description into the
`custom.size_chart_data` metafield, so the bot (and the storefront theme) read
it as a chart instead of as a paragraph of HTML.

Rehla's descriptions carry a table like

    <h3>Top Size Chart</h3><table><tr><th>Size</th><th>Width (cm)</th>
    <th>Length (cm)</th></tr><tr><td>Large</td><td>36</td><td>54</td></tr>...

The measurement table becomes the chart's columns (the garment laid flat, in
centimetres). The "Recommended Weight" table beside it is advice about the
*wearer*, so it is kept apart, as `fit.recommended_weight_kg` -- `{"S": [45,
55], ...}` -- which the bot matches a customer's weight against. A table whose
cells are not plain numbers is skipped, never guessed at.

Dry run by default. Writes only the data metafield (no picture), through the
same `set_product_chart` the dashboard uses. A product that already has the
metafield is left alone unless `--update`, which rewrites it when what the
description says differs.

    python scripts/shopify_size_charts_from_descriptions.py
    python scripts/shopify_size_charts_from_descriptions.py --apply [--update]

Afterwards `scripts/shopify_size_charts_import.py --apply` (run against the
database the bot reads) brings the charts into `size_charts`.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations.shopify import size_charts  # noqa: E402
from integrations.shopify.client import get_admin_client  # noqa: E402

READ = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id handle title descriptionHtml
      chart: metafield(namespace: "custom", key: "size_chart_data") { value }
    }
  }
}
"""

SIZE_NAMES = {
    "xs": "XS", "extra small": "XS", "s": "S", "small": "S", "m": "M", "medium": "M",
    "l": "L", "large": "L", "xl": "XL", "extra large": "XL",
}
ORDER = ["XS", "S", "M", "L", "XL"]

#: header text -> the measurement it is (key, English, Arabic, marker).
COLUMNS = {
    "width": ("width", "Width", "العرض", "A"),
    "length": ("length", "Length", "الطول", "B"),
    "sleeve": ("sleeve", "Sleeve", "الكم", "C"),
}

TABLE = re.compile(r"<h3>([^<]*)</h3>\s*<table>(.*?)</table>", re.S | re.I)
CELL = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)


def _text(cell: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()


WEIGHT = re.compile(r"(\d+)\s*[–-]\s*(\d+)\s*kg", re.I)


def parse_weights(description: str) -> dict[str, list[int]] | None:
    """`{"S": [45, 55], ...}` from a "Recommended Weight" table, or None."""
    for heading, body in TABLE.findall(description or ""):
        if "weight" not in heading.lower():
            continue
        out: dict[str, list[int]] = {}
        for row in ROW.findall(body):
            cells = [_text(c) for c in CELL.findall(row)]
            if len(cells) != 2 or cells[0].lower() == "size":
                continue
            name = SIZE_NAMES.get(cells[0].strip().lower())
            found = WEIGHT.search(cells[1])
            if name is None or found is None:
                return None
            out[name] = [int(found.group(1)), int(found.group(2))]
        return {k: out[k] for k in ORDER if k in out} or None
    return None


def parse(description: str) -> dict | None:
    """The first table that is plain garment measurements, as a chart dict."""
    for heading, body in TABLE.findall(description or ""):
        rows = [[_text(c) for c in CELL.findall(r)] for r in ROW.findall(body)]
        rows = [r for r in rows if r]
        if len(rows) < 2 or rows[0][0].lower() != "size":
            continue
        columns = []
        for head in rows[0][1:]:
            word = re.sub(r"\(.*?\)", "", head).strip().lower()
            if word not in COLUMNS or "(cm)" not in head.lower():
                columns = None
                break
            columns.append(COLUMNS[word])
        if not columns:
            continue
        sizes: dict[str, dict] = {}
        for row in rows[1:]:
            name = SIZE_NAMES.get(row[0].strip().lower())
            if name is None or len(row) != len(columns) + 1:
                return None
            try:
                sizes[name] = {
                    c[0]: float(v) if "." in v else int(v)
                    for c, v in zip(columns, row[1:], strict=True)
                }
            except ValueError:
                return None
        if not sizes:
            return None
        ordered = {k: sizes[k] for k in ORDER if k in sizes}
        weights = parse_weights(description)
        return {
            **({"fit": {"recommended_weight_kg": weights}} if weights else {}),
            "chart_id": None,
            "title": heading.strip(),
            "unit": "cm",
            "measurements": [
                {"key": k, "label_en": en, "label_ar": ar, "marker": mk} for k, en, ar, mk in columns
            ],
            "sizes": ordered,
        }
    return None


def products() -> list[dict]:
    client = get_admin_client()
    out, cursor = [], None
    while True:
        page = client(READ, {"cursor": cursor})["products"]
        out.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--update", action="store_true", help="rewrite an existing metafield that differs")
    args = ap.parse_args()

    todo = []
    for p in products():
        chart = parse(p["descriptionHtml"])
        if chart is None:
            continue
        chart["chart_id"] = f"shopify-{p['id'].rsplit('/', 1)[-1]}"
        existing = (p.get("chart") or {}).get("value")
        if existing:
            same = json.loads(existing) == size_charts.storefront_payload(chart)
            if same or not args.update:
                state = "matches" if same else "set (use --update)"
                print(f"  skip {p['handle']}: metafield already {state}")
                continue
        todo.append((p, chart))

    print(f"products with a measurement table in the description: {len(todo)}")
    for p, chart in todo:
        print(f"  {p['handle']}: {chart['title']!r}")
        for size, values in chart["sizes"].items():
            weight = (chart.get("fit") or {}).get("recommended_weight_kg", {}).get(size)
            print(f"      {size}: {values}" + (f"  weight {weight[0]}-{weight[1]} kg" if weight else ""))
    if not args.apply:
        print("\ndry run: nothing written. Re-run with --apply.")
        return
    created = size_charts.ensure_definitions(apply=True)
    print(f"metafield definitions created: {created or 'none, already there'}")
    for p, chart in todo:
        size_charts.set_product_chart(p["id"], chart)
        print(f"  wrote {p['handle']}")


if __name__ == "__main__":
    main()
