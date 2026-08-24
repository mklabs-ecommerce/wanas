"""Size charts.

Ships as `data/size_charts.json` and stays a file in Phase 1 -- 12 rows that
change rarely (16-supporting-tables.md). Everything reads it through this
module, so it becomes a table later without touching the tool.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from config.settings import DATA_DIR

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


def all_charts() -> dict:
    return _load()


def get_chart(chart_id: str | None) -> dict | None:
    if not chart_id:
        return None
    return _load().get(chart_id)
