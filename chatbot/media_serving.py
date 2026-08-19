"""Serving a catalog file to a browser, safely.

Shared by the harness and the staff dashboard -- the two places a person
looks at a conversation and needs an attachment to render as a picture
instead of a bare path. Not for product photos that are already a Shopify
CDN url (`backend/integrations/whatsapp_client.py::_is_url`); those are
already something a browser can load directly.
"""

from __future__ import annotations

from pathlib import Path

from backend.config import PROJECT_ROOT

#: Only these roots may be served, so a crafted path cannot walk out of the
#: project. `data/inbound` is what a customer's own photo or voice note
#: downloads into; the other two are catalog assets.
SERVABLE_ROOTS = ("data/size-charts", "data/images", "data/inbound")


def resolve_servable_path(path: str) -> Path | None:
    """The real file for `path`, or None if it is outside the allowed roots
    or does not exist. `resolve()` collapses any ``..`` before the containment
    check runs, so that check is the actual guard, not the prefix match above
    it."""
    normalised = (path or "").replace("\\", "/").lstrip("/")
    if not any(normalised.startswith(root) for root in SERVABLE_ROOTS):
        return None
    target = (PROJECT_ROOT / normalised).resolve()
    if not str(target).startswith(str(PROJECT_ROOT.resolve())) or not target.is_file():
        return None
    return target
