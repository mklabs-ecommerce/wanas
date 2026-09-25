"""Size charts.

Two sources, one merged view.

`data/size_charts.json` holds the twelve charts this shop shipped with. They
are versioned with the code and change rarely, which is what a file is good
at, and it stays the default.

It is a poor home for a chart made from the dashboard at 11pm, though: a file
written on Railway does not survive the next deploy, and a chart nobody can
add without a pull request is a chart nobody adds. Those live in the
`size_charts` table (`domain.models.SizeChart`).

So the two overlay, the same way Shopify's live price overlays `wanas.db`'s
in `catalog._overlay`: **the file is the default, a row wins on the same
`chart_id`.** Everything -- the bot's `get_size_chart` tool, the dashboard,
and the Shopify metafield publisher -- reads through here, so none of them
has to know which side a chart came from.

Pass a `Session` to see the database half. Without one you get the file only,
which is what `manage.py` and the offline scripts want.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.servable_paths import resolve_public_path
from config.settings import DATA_DIR
from domain.models import Product, SizeChart

log = logging.getLogger("wanas.size_charts")

CHARTS_PATH = DATA_DIR / "size_charts.json"

#: Supplied by the tool, not stored per chart, so every chart answers the same
#: shape. A customer who reads a 31 cm waist as a body measurement concludes
#: the trousers are for a child -- the misreading is predictable, so the note
#: is unconditional.
MEASUREMENT_NOTE = "Garment measurements laid flat, not body measurements."


@lru_cache(maxsize=1)
def _load(path: str | None = None) -> dict:
    with open(Path(path) if path else CHARTS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def as_chart(row: SizeChart) -> dict:
    """A `SizeChart` row in the same shape `size_charts.json` uses.

    The point of the shape being identical is that nothing downstream branches
    on where a chart came from -- `storefront_payload`, the bot's tool and the
    dashboard all take one kind of dict.
    """
    return {
        "chart_id": row.chart_id,
        "title": row.title or row.chart_id,
        "image": row.image_url,
        "unit": row.unit or "cm",
        "measurements": list(row.measurements or []),
        "sizes": dict(row.sizes or {}),
        "source": row.source or "manual",
    }


def all_charts(session: Session | None = None) -> dict:
    """Every chart, file and database, database winning on a shared id."""
    charts = dict(_load())
    if session is not None:
        for row in session.scalars(select(SizeChart)).all():
            charts[row.chart_id] = as_chart(row)
    return charts


def get_chart(chart_id: str | None, session: Session | None = None) -> dict | None:
    if not chart_id:
        return None
    if session is not None:
        row = session.get(SizeChart, chart_id)
        if row is not None:
            return as_chart(row)
    return _load().get(chart_id)


def sendable_image(image: str | None) -> str | None:
    """`image` if a customer can actually be sent it, otherwise None.

    A hosted picture (Shopify Files, a CDN) is sent by link and Meta fetches
    it. A local one has to be a real file under the public catalog roots --
    `wns-boxy-tee.png` was named by the chart file and never committed, and a
    path to nothing attached to a reply is a chart promised to the customer
    that can only fail on send.
    """
    if not (isinstance(image, str) and image.strip()):
        return None
    if image.startswith(("http://", "https://")):
        return image
    return image if resolve_public_path(image) is not None else None


def chart_picture(product: Product, chart: dict | None) -> str | None:
    """The size-chart picture to send for one product, or None.

    The chart's own picture first, then the one uploaded for the product
    itself (`Product.size_chart_image` -- the dashboard's upload, or Shopify's
    `custom.size_chart`). Both are checked with `sendable_image`, so a chart
    whose file is missing falls through to the product's own picture rather
    than to nothing -- and never to a neighbouring product's.
    """
    named = (chart or {}).get("image")
    picture = sendable_image(named)
    if picture is None and named:
        log.error(
            "size chart %r for %s names a picture that is not there: %s",
            (chart or {}).get("chart_id"),
            product.product_id,
            named,
        )
    return picture or sendable_image(product.size_chart_image)
