"""A public, token-gated media route -- built for one consumer: Meta's own
fetcher.

Instagram cannot be sent an uploaded image. An outbound picture is a **public
HTTPS URL** that Meta's servers fetch themselves, and Meta's fetcher has no
staff cookie and no session token. The size charts live on local disk behind
the dashboard login, so without this route the bot literally cannot answer a
sizing question on Instagram.

The gate is an HMAC of the requested path under `MEDIA_URL_SECRET` (falling
back to `DASHBOARD_SESSION_SECRET`), truncated to 32 hex chars:

    GET /public/media/{token}/{filename:path}

Properties, all deliberate:

* **Deterministic per path** -- the same size chart always yields the same
  URL, so Meta's own caching works and nothing here needs a database.
* **404, never 403, on anything wrong** -- a bad token, an unknown file, a
  traversal attempt and a disallowed root are indistinguishable from the
  outside; confirming that *something* exists at a guessed path is worth
  nothing to anyone.
* **`data/inbound` is unreachable through this route even with a correctly
  computed token for it** (`common/servable_paths.py::PUBLIC_ROOTS`). Those
  are customers' own photos and voice notes; the public internet never gets
  them, whatever else this route might otherwise be convenient for.
* With no secret configured every request 404s and `public_url_for` returns
  None -- the same "no secret means refuse" contract as the Shopify webhook
  and the dashboard login.

Mounted unconditionally in `app.py`: it is safe by construction (nothing but
catalog assets behind an HMAC gate) and it has none of the dashboard/harness
gates because it needs none of them.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse

from common.servable_paths import resolve_public_path
from config.settings import settings

log = logging.getLogger("wanas.public_media")

router = APIRouter(prefix="/public", tags=["public-media"])

#: One week. The URLs are deterministic, so a long cache is safe and keeps
#: Meta's fetcher from re-pulling a chart on every sizing question.
CACHE_CONTROL = "public, max-age=604800"


def media_token(path: str) -> str | None:
    """The deterministic token for one servable path, or None with no secret."""
    secret = settings.media_url_secret
    if not secret:
        return None
    normalised = (path or "").replace("\\", "/").lstrip("/")
    return hmac.new(
        secret.encode("utf-8"), normalised.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def public_url_for(path: str) -> str | None:
    """The URL handed to Meta for a local catalog file, or None when there is
    no public base URL to build it on, no signing secret, or the path is not
    publicly servable. `None` is what makes `InstagramClient.send_image`
    return `delivered=False` rather than post a URL Meta cannot reach."""
    if not settings.public_base_url:
        return None
    normalised = (path or "").replace("\\", "/").lstrip("/")
    if media_token(normalised) is None or resolve_public_path(normalised) is None:
        return None
    token = media_token(normalised)
    return f"{settings.public_base_url}/public/media/{token}/{normalised}"


def _not_found() -> Response:
    return Response(content="not found", status_code=404, media_type="text/plain")


@router.get("/media/{token}/{filename:path}")
def media(token: str, filename: str):
    expected = media_token(filename)
    if expected is None or not token or not hmac.compare_digest(expected, token):
        # A tampered token must not confirm whether the file exists.
        log.info("rejected a public media request with a bad token")
        return _not_found()
    target = resolve_public_path(filename)
    if target is None:
        # Includes data/inbound paths even under a correctly computed token:
        # the roots check is the guard, the token only authenticates.
        return _not_found()
    return FileResponse(target, headers={"Cache-Control": CACHE_CONTROL})
