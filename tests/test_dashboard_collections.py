"""Collections: list, create, edit, and the smart-collection refusal.

The one rule with teeth here is the last one. A smart collection's members
come from its rules, so accepting a hand edit would mean telling staff a
product was added and letting Shopify silently drop it on the next
re-evaluation -- worse than refusing.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import collections_api, web as dashboard
from domain.services import auth

SECRET = "test-dashboard-secret"


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(collections_api.router)
    return TestClient(app)


@pytest.fixture()
def logged_in(client, seeded):
    auth.create_staff(seeded, "sara", "correct horse battery")
    seeded.commit()
    res = client.post(
        "/dashboard/api/login", json={"username": "sara", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text
    return client


@pytest.fixture()
def product_gid(shopify):
    return next(iter(shopify.products))


def test_requires_login(client):
    assert client.get("/dashboard/api/shopify/collections").status_code == 401


def test_lists_collections(logged_in, shopify):
    shopify.seed_collection("شتوي ٢٠٢٦")
    body = logged_in.get("/dashboard/api/shopify/collections").json()
    assert [c["title"] for c in body["collections"]] == ["شتوي ٢٠٢٦"]
    assert body["collections"][0]["smart"] is False


def test_creates_a_collection(logged_in, shopify):
    res = logged_in.post("/dashboard/api/shopify/collections", json={"title": "صيف ٢٠٢٦"})
    assert res.status_code == 201
    assert res.json()["title"] == "صيف ٢٠٢٦"
    assert any(c["title"] == "صيف ٢٠٢٦" for c in shopify.collections.values())


def test_create_needs_a_title(logged_in, shopify):
    res = logged_in.post("/dashboard/api/shopify/collections", json={"title": "  "})
    assert res.status_code == 400


def test_a_shopify_refusal_is_a_409_not_an_outage(logged_in, shopify):
    shopify.seed_collection("مكررة")
    res = logged_in.post("/dashboard/api/shopify/collections", json={"title": "مكررة"})
    assert res.status_code == 409
    assert res.json()["error"] == "collection_rejected"


def test_detail_lists_member_products(logged_in, shopify, product_gid):
    gid = shopify.seed_collection("الأساسيات", product_ids=[product_gid])
    body = logged_in.get(f"/dashboard/api/shopify/collections/{gid}").json()
    assert body["product_count"] == 1
    assert body["products"][0]["id"] == product_gid


def test_detail_404s_for_a_missing_collection(logged_in, shopify):
    res = logged_in.get("/dashboard/api/shopify/collections/gid://shopify/Collection/nope")
    assert res.status_code == 404


def test_update_renames(logged_in, shopify):
    gid = shopify.seed_collection("قديم")
    res = logged_in.post(f"/dashboard/api/shopify/collections/{gid}/update", json={"title": "جديد"})
    assert res.status_code == 200
    assert shopify.collections[gid]["title"] == "جديد"


def test_add_and_remove_products(logged_in, shopify, product_gid):
    gid = shopify.seed_collection("يدوية")

    add = logged_in.post(
        f"/dashboard/api/shopify/collections/{gid}/products/add", json={"product_ids": [product_gid]}
    )
    assert add.status_code == 200
    assert shopify.collections[gid]["product_ids"] == [product_gid]

    remove = logged_in.post(
        f"/dashboard/api/shopify/collections/{gid}/products/remove", json={"product_ids": [product_gid]}
    )
    assert remove.status_code == 200
    assert shopify.collections[gid]["product_ids"] == []


def test_membership_needs_product_ids(logged_in, shopify):
    gid = shopify.seed_collection("يدوية")
    res = logged_in.post(f"/dashboard/api/shopify/collections/{gid}/products/add", json={"product_ids": []})
    assert res.status_code == 400


def test_a_smart_collection_refuses_a_hand_edit(logged_in, shopify, product_gid):
    gid = shopify.seed_collection("ذكية", smart=True)
    res = logged_in.post(
        f"/dashboard/api/shopify/collections/{gid}/products/add", json={"product_ids": [product_gid]}
    )
    assert res.status_code == 409
    assert res.json()["error"] == "smart_collection"
    assert shopify.collections[gid]["product_ids"] == []


def test_outage_is_a_503(logged_in, shopify):
    shopify.down = True
    assert logged_in.get("/dashboard/api/shopify/collections").status_code == 503
