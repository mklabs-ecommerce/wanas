"""The Shopify Products section of the staff dashboard: view, create, edit.

`shopify_admin_products.py`'s own tests already pin down the create/update
orchestration and local mirroring; these tests are the thinner layer on top
-- auth, request shaping, and status codes -- the same split
`test_dashboard_orders.py` uses for the orders section.
"""

from __future__ import annotations

import base64
import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import shopify_api, web as dashboard
from domain.models import Product
from domain.services import auth

SECRET = "test-dashboard-secret"
VARIANT = "wanas-hoodie-s-olive"


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(shopify_api.router)
    return TestClient(app)


@pytest.fixture()
def staff(seeded):
    person = auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    return person


@pytest.fixture()
def logged_in(client, staff):
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


# --------------------------------------------------------------------------
# list / detail
# --------------------------------------------------------------------------


def test_products_list_requires_login(client):
    assert client.get("/dashboard/api/shopify/products").status_code == 401


def test_products_list_includes_the_seeded_catalog(logged_in, seeded, shopify):
    res = logged_in.get("/dashboard/api/shopify/products")
    assert res.status_code == 200
    titles = {p["title"] for p in res.json()["products"]}
    assert "WANAS Hoodie" in titles


def test_products_list_reports_an_outage(logged_in, shopify):
    shopify.down = True
    assert logged_in.get("/dashboard/api/shopify/products").status_code == 503


def test_product_detail_includes_local_metadata_when_linked(logged_in, shopify):
    gid = shopify.variant_to_product[VARIANT]
    res = logged_in.get(f"/dashboard/api/shopify/products/{gid}")
    assert res.status_code == 200
    body = res.json()
    assert body["local_product_id"] == "wanas-hoodie"
    assert body["local"]["department"] == "unisex"
    assert any(v["sku"] == VARIANT for v in body["variants"])


def test_product_detail_404s_for_an_unknown_product(logged_in):
    res = logged_in.get("/dashboard/api/shopify/products/gid://shopify/Product/999999")
    assert res.status_code == 404


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def test_creating_a_product_requires_login(client):
    res = client.post("/dashboard/api/shopify/products", json={"title": "x"})
    assert res.status_code == 401


def test_creating_a_product_rejects_missing_fields(logged_in):
    res = logged_in.post("/dashboard/api/shopify/products", json={"title": "Beanie"})
    assert res.status_code == 400


def test_creating_a_product_end_to_end(logged_in, seeded, shopify):
    res = logged_in.post(
        "/dashboard/api/shopify/products",
        json={
            "title": "Dashboard Beanie",
            "description": "warm",
            "category": "Tops",
            "department": "unisex",
            "variants": [{"size": "One Size", "price": 250, "stock_qty": 10}],
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["product_id"] == "dashboard-beanie"

    detail = logged_in.get(f"/dashboard/api/shopify/products/{body['shopify_id']}")
    assert detail.status_code == 200
    assert detail.json()["local_product_id"] == "dashboard-beanie"
    assert shopify.qty("dashboard-beanie-one-size") == 10


def test_creating_a_product_with_no_variants_is_rejected(logged_in):
    res = logged_in.post(
        "/dashboard/api/shopify/products",
        json={"title": "Empty", "category": "Tops", "department": "unisex", "variants": []},
    )
    assert res.status_code == 400  # caught by the required-fields check (empty list)


# --------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------


def test_updating_a_product_requires_login(client):
    res = client.post("/dashboard/api/shopify/products/wanas-hoodie/update", json={})
    assert res.status_code == 401


def test_updating_a_product_changes_local_and_shopify_fields(logged_in, shopify):
    res = logged_in.post(
        "/dashboard/api/shopify/products/wanas-hoodie/update",
        json={"collection": "WINTER COLLECTION", "title": "WANAS Hoodie 2.0"},
    )
    assert res.status_code == 200, res.text

    gid = shopify.variant_to_product[VARIANT]
    assert shopify.products[gid]["title"] == "WANAS Hoodie 2.0"

    detail = logged_in.get(f"/dashboard/api/shopify/products/{gid}")
    assert detail.json()["local"]["collection"] == "WINTER COLLECTION"


def test_updating_an_unknown_product_404s(logged_in):
    res = logged_in.post("/dashboard/api/shopify/products/not-a-product/update", json={})
    assert res.status_code == 404


def test_updating_variant_price_and_stock(logged_in, shopify):
    res = logged_in.post(
        "/dashboard/api/shopify/products/wanas-hoodie/update",
        json={"variant_updates": [{"variant_id": VARIANT, "price": 777, "stock_qty": 3}]},
    )
    assert res.status_code == 200, res.text
    assert shopify.qty(VARIANT) == 3


# --------------------------------------------------------------------------
# pictures and the size chart, off a staff member's laptop
# --------------------------------------------------------------------------

#: The smallest thing a browser will hand over that is really a PNG.
PNG = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
).decode()


@pytest.fixture()
def uploads(monkeypatch):
    """Shopify's staged-upload dance, recorded rather than performed.

    The dance itself is three HTTP calls to two hosts; what these tests are
    about is which door a purpose goes through -- a product photo staged and
    left for the product to own, a size chart put in the Files library where a
    `file_reference` metafield can point at it.
    """
    calls = {"staged": [], "files": []}

    def stage(client, filename, data, mime, *, resource="FILE"):
        calls["staged"].append({"filename": filename, "mime": mime, "resource": resource, "size": len(data)})
        return f"https://staged.example/{filename}"

    def upload_to_files(client, filename, data, mime, *, alt=""):
        calls["files"].append({"filename": filename, "mime": mime, "alt": alt})
        return {"id": f"gid://shopify/MediaImage/{filename}", "url": f"https://cdn.example/{filename}"}

    monkeypatch.setattr(shopify_api.shopify_files, "stage", stage)
    monkeypatch.setattr(shopify_api.shopify_files, "upload_to_files", upload_to_files)
    monkeypatch.setattr(shopify_api, "get_admin_client", lambda: object())
    return calls


def test_uploading_a_picture_requires_login(client):
    res = client.post("/dashboard/api/shopify/uploads", json={"filename": "a.png", "content_type": "image/png", "data": PNG})
    assert res.status_code == 401


def test_a_product_photo_is_staged_and_left_for_the_product_to_own(logged_in, uploads):
    res = logged_in.post(
        "/dashboard/api/shopify/uploads",
        json={"filename": "olive hoodie.PNG", "content_type": "image/png", "data": PNG,
              "purpose": "product_image"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"].startswith("https://staged.example/")
    assert uploads["staged"][0]["resource"] == "IMAGE"
    assert uploads["files"] == []


def test_a_size_chart_goes_into_the_files_library_because_a_metafield_needs_a_gid(logged_in, uploads):
    res = logged_in.post(
        "/dashboard/api/shopify/uploads",
        json={"filename": "chart.png", "content_type": "image/png", "data": PNG, "purpose": "size_chart"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["file_gid"].startswith("gid://shopify/MediaImage/")
    assert uploads["staged"] == []
    assert uploads["files"][0]["filename"] == "chart.png"


def test_a_filename_reaches_shopify_rebuilt_not_filtered(logged_in, uploads):
    """It arrives from a staff member's filesystem and ends up in a url."""
    logged_in.post(
        "/dashboard/api/shopify/uploads",
        json={"filename": "../../etc/passwd; rm -rf.png", "content_type": "image/png", "data": PNG},
    )
    assert uploads["staged"][0]["filename"] == "etc-passwd-rm--rf.png"


def test_an_svg_is_refused_because_files_serves_it_on_the_shops_own_origin(logged_in, uploads):
    res = logged_in.post(
        "/dashboard/api/shopify/uploads",
        json={"filename": "x.svg", "content_type": "image/svg+xml", "data": PNG},
    )
    assert res.status_code == 400
    assert uploads["staged"] == [] and uploads["files"] == []


def test_something_that_is_not_base64_is_a_bad_request_not_a_crash(logged_in, uploads):
    res = logged_in.post(
        "/dashboard/api/shopify/uploads",
        json={"filename": "x.png", "content_type": "image/png", "data": "not base64!!"},
    )
    assert res.status_code == 400


def test_a_file_over_the_cap_is_refused_before_it_reaches_shopify(logged_in, uploads, monkeypatch):
    monkeypatch.setattr(shopify_api, "MAX_UPLOAD_BYTES", 10)
    res = logged_in.post(
        "/dashboard/api/shopify/uploads",
        json={"filename": "x.png", "content_type": "image/png", "data": base64.b64encode(b"x" * 40).decode()},
    )
    assert res.status_code == 413
    assert uploads["staged"] == []


def test_the_chart_picker_offers_only_charts_that_exist(logged_in):
    """A free-text chart id was a way to type one the bot then answers "no
    chart" to."""
    res = logged_in.get("/dashboard/api/shopify/size-charts")
    assert res.status_code == 200
    charts = res.json()["charts"]
    assert charts and all(c["id"] and c["title"] for c in charts)
    assert "wide-leg-sweatpants" in {c["id"] for c in charts}


def test_the_chart_picker_requires_login(client):
    assert client.get("/dashboard/api/shopify/size-charts").status_code == 401


def test_creating_a_product_carries_its_photos_and_its_chart(logged_in, seeded, shopify):
    res = logged_in.post(
        "/dashboard/api/shopify/products",
        json={
            "title": "Dashboard Tee",
            "category": "Tops",
            "department": "unisex",
            "variants": [
                {"size": "S", "color": "Olive", "price": 300, "stock_qty": 4},
                {"size": "M", "color": "Navy", "price": 300, "stock_qty": 2},
            ],
            "images": [
                {"color": "Olive", "source": "https://staged.example/olive.png"},
                {"color": "Navy", "source": "https://staged.example/navy.png"},
            ],
            "size_chart_file_gid": "gid://shopify/MediaImage/chart.png",
            "size_chart_url": "https://cdn.example/chart.png",
        },
    )
    assert res.status_code == 201, res.text

    product = seeded.get(Product, "dashboard-tee")
    assert product.color_images == {
        "Olive": ["https://staged.example/olive.png"],
        "Navy": ["https://staged.example/navy.png"],
    }
    assert product.size_chart_image == "https://cdn.example/chart.png"
    assert shopify.chart_metafields[res.json()["shopify_id"]] == "gid://shopify/MediaImage/chart.png"
    assert shopify.variant_images["dashboard-tee-s-olive"] == "https://staged.example/olive.png"
    assert shopify.variant_images["dashboard-tee-m-navy"] == "https://staged.example/navy.png"
