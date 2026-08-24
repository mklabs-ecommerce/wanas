"""The Shopify Products section of the staff dashboard: view, create, edit.

`shopify_admin_products.py`'s own tests already pin down the create/update
orchestration and local mirroring; these tests are the thinner layer on top
-- auth, request shaping, and status codes -- the same split
`test_dashboard_orders.py` uses for the orders section.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services import auth
from config.settings import settings
from dashboard import shopify_api
from dashboard import web as dashboard

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
