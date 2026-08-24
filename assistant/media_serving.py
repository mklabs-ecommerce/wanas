"""Serving a catalog file to a browser, safely.

Shared by the harness and the staff dashboard -- the two places a person
looks at a conversation and needs an attachment to render as a picture
instead of a bare path. Not for product photos that are already a Shopify
CDN url (`integrations/whatsapp/client.py::_is_url`); those are
already something a browser can load directly.

The roots and the containment check live in `common/servable_paths.py` so
the public media route (`api/public_media.py`) can share the exact same
guard without domain/ importing assistant/. Re-exported here under the
names every consumer has always used.
"""

from __future__ import annotations

from common.servable_paths import (  # noqa: F401 -- re-exported
    PUBLIC_ROOTS,
    SERVABLE_ROOTS,
    resolve_public_path,
    resolve_servable_path,
)
