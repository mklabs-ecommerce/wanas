"""Shared webhook-signature verification for Meta's platforms.

Every Meta webhook this app serves -- WhatsApp Cloud API today, Instagram
(with the Instagram Login flavour) later -- signs its requests the same way:
a hex HMAC-SHA256 of the raw request body under `X-Hub-Signature-256`, keyed
with that product's own app secret. One implementation here means neither
adapter can drift from the other; a signature check that drifts is a webhook
that either refuses real traffic or accepts forged requests.

Shopify signs differently (base64 HMAC-SHA256) and keeps its own check in
`integrations/shopify/webhooks.py`.
"""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Confirms the request genuinely came from Meta, not "any request that
    showed up"."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])
