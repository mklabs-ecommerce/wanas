"""The privacy policy page.

Meta's app review fetches this URL while logged out, and a WhatsApp app whose
policy URL 404s or redirects to a login cannot be submitted. That is the whole
reason these assertions exist: the page must answer anonymously, and it must
still name the third parties customer data actually reaches.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import legal


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(legal.router)
    return TestClient(app)


def test_privacy_page_is_public_html():
    response = _client().get("/privacy")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Privacy Policy" in response.text


def test_privacy_page_names_every_processor_and_a_contact():
    """If a processor is added to the system and not to the page, the page has
    become untrue -- which is worse than not having one."""
    text = _client().get("/privacy").text

    for processor in ("WhatsApp", "Google", "Shopify", "Railway"):
        assert processor in text

    assert legal.CONTACT_EMAIL in text
    assert legal.LAST_UPDATED in text
