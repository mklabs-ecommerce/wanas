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
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import DATA_DIR
from domain.models import SizeChart

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
