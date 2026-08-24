"""The public media route -- the one place the project's files are reachable
without a login.

The customers'-photos test is the point of the whole module: `data/inbound`
holds what customers sent *us*, and it must stay unreachable from the public
internet even for a caller who can compute valid tokens. Everything else here
is the usual gate behaviour: right token serves, wrong token 404s, traversal
404s.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import public_media
from config.settings import settings

SECRET = "test-media-secret"
BASE = "https://wanas.example.com"
CHART = "data/size-charts/wide-leg-sweatpants.png"


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(
        public_media,
        "settings",
        dataclasses.replace(settings, public_base_url=BASE, media_url_secret=SECRET),
    )


@pytest.fixture()
def fake(monkeypatch):
    from tests.fake_instagram import FakeInstagram

    return FakeInstagram().install(monkeypatch)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(public_media.router)
    return TestClient(app)


def token_for(path: str) -> str:
    return public_media.media_token(path)


def test_a_valid_token_serves_the_file_with_the_cache_header(client):
    response = client.get(f"/public/media/{token_for(CHART)}/{CHART}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=604800"


def test_the_url_is_deterministic_per_path(configured):
    assert public_media.public_url_for(CHART) == public_media.public_url_for(CHART)
    assert public_media.public_url_for(CHART).startswith(f"{BASE}/public/media/")
    # A different file gets a different token.
    other = public_media.public_url_for("data/size-charts/zipup.png")
    assert other != public_media.public_url_for(CHART)


def test_a_tampered_token_is_a_404_not_a_403(client):
    good = token_for(CHART)
    tampered = ("0" if good[0] != "0" else "1") + good[1:]
    response = client.get(f"/public/media/{tampered}/{CHART}")
    assert response.status_code == 404


def test_data_inbound_is_never_servable_even_with_a_correct_token(client):
    """The customers'-photos test. A token computed *correctly* for an inbound
    path buys nothing: PUBLIC_ROOTS is the guard, the token only authenticates."""
    inbound = "data/inbound/some-customers-photo.jpg"
    assert token_for(inbound) is not None  # the secret exists; the token computes
    response = client.get(f"/public/media/{token_for(inbound)}/{inbound}")
    assert response.status_code == 404


def test_traversal_is_a_404(client):
    escaped = "../config/settings.py"
    response = client.get(f"/public/media/{token_for(CHART)}/{escaped}")
    assert response.status_code == 404


def test_traversal_that_keeps_a_valid_prefix_still_404s(client):
    """A naive `str.startswith("data/size-charts")` plus a PROJECT_ROOT-only
    containment check would let this through: the string starts with an
    allowed root, and after `..` collapses it still lands inside the repo --
    just inside `data/inbound`, not `data/size-charts`. The token has to be
    computed for this exact escaped string, which only proves the containment
    check -- not the HMAC -- is what is being tested here."""
    escaped = "data/size-charts/../inbound/some-customers-photo.jpg"
    response = client.get(f"/public/media/{token_for(escaped)}/{escaped}")
    assert response.status_code == 404


def test_no_secret_configured_means_everything_404s_and_no_urls_are_built(
    monkeypatch,
):
    monkeypatch.setattr(
        public_media,
        "settings",
        dataclasses.replace(settings, public_base_url=BASE, media_url_secret=""),
    )
    app = FastAPI()
    app.include_router(public_media.router)
    client = TestClient(app)

    assert public_media.media_token(CHART) is None
    assert public_media.public_url_for(CHART) is None
    assert client.get(f"/public/media/anything/{CHART}").status_code == 404


# --- the InstagramClient side ---------------------------------------------


def _client():
    from integrations.instagram.client import InstagramClient

    return InstagramClient(account_id="17841400000000000", access_token="test-token")


def test_send_image_of_a_local_chart_posts_its_public_url(fake, configured):
    result = _client().send_image("1234567890", CHART, caption="مقاسات السويتبانتس")

    assert result.delivered is True
    payloads = fake.message_payloads()
    image_payload = payloads[-1]["message"]["attachment"]
    assert image_payload["type"] == "image"
    assert image_payload["payload"]["url"].endswith(f"/{CHART}")
    assert "/public/media/" in image_payload["payload"]["url"]
    # The caption went first, as its own text message: Instagram attachments
    # carry no caption field.
    assert fake.texts()[-1] == "مقاسات السويتبانتس"
    texts_before_image = [p["message"].get("text") for p in payloads[:-1]]
    assert "مقاسات السويتبانتس" in texts_before_image


def test_send_image_without_a_public_base_url_refuses_instead_of_posting(
    fake, monkeypatch
):
    monkeypatch.setattr(
        public_media,
        "settings",
        dataclasses.replace(settings, public_base_url="", media_url_secret=SECRET),
    )
    result = _client().send_image("1234567890", CHART)

    assert result.delivered is False
    assert result.error == "no_public_base_url"
    assert fake.calls == []  # nothing posted that Meta could not fetch


def test_send_image_passes_a_shopify_url_straight_through(fake, configured):
    url = "https://cdn.shopify.com/s/files/1/hoodie-olive.jpg"
    result = _client().send_image("1234567890", url)

    assert result.delivered is True
    assert fake.message_payloads()[-1]["message"]["attachment"]["payload"]["url"] == url
