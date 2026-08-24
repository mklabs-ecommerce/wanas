"""The Inventory section: the flat per-variant view and the batched write.

`dashboard/inventory_api.py` is the only place in the dashboard that shows
stock as its own subject rather than a column on a product, and the only one
that writes stock without going through a product edit. The two things worth
pinning down here are that the header totals are computed over the *whole*
store (a filter must not silently change what a labelled tile means) and that
the write is absolute, never a delta.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import inventory_api, web as dashboard
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
    app.include_router(inventory_api.router)
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


def test_inventory_requires_login(client):
    assert client.get("/dashboard/api/shopify/inventory").status_code == 401


def test_inventory_lists_variants_worst_first(logged_in, shopify):
    shopify.set(VARIANT, qty=0)
    body = logged_in.get("/dashboard/api/shopify/inventory").json()

    quantities = [row["quantity"] for row in body["rows"]]
    assert quantities == sorted(quantities)
    assert body["rows"][0]["quantity"] == 0


def test_totals_are_store_wide_not_filtered(logged_in, shopify):
    shopify.set(VARIANT, qty=0)
    unfiltered = logged_in.get("/dashboard/api/shopify/inventory").json()
    filtered = logged_in.get("/dashboard/api/shopify/inventory?status=out").json()

    assert filtered["totals"] == unfiltered["totals"]
    assert filtered["match_count"] < unfiltered["match_count"]
    assert all(row["quantity"] <= 0 for row in filtered["rows"])


def test_low_stock_threshold_is_honoured(logged_in, shopify):
    shopify.set(VARIANT, qty=2)
    body = logged_in.get("/dashboard/api/shopify/inventory?status=low&low_stock_at=2").json()

    assert VARIANT in {row["sku"] for row in body["rows"]}
    assert body["totals"]["low_stock_at"] == 2

    stricter = logged_in.get("/dashboard/api/shopify/inventory?status=low&low_stock_at=1").json()
    assert VARIANT not in {row["sku"] for row in stricter["rows"]}


def test_search_matches_sku_and_colour(logged_in, shopify):
    body = logged_in.get("/dashboard/api/shopify/inventory?q=olive").json()
    assert body["rows"]
    assert all("olive" in row["sku"].lower() or (row["color"] or "").lower() == "olive"
               for row in body["rows"])


def test_bad_status_is_refused(logged_in):
    res = logged_in.get("/dashboard/api/shopify/inventory?status=nope")
    assert res.status_code == 400


def test_outage_is_a_503(logged_in, shopify):
    shopify.down = True
    assert logged_in.get("/dashboard/api/shopify/inventory").status_code == 503


# --------------------------------------------------------------------------
# writing stock
# --------------------------------------------------------------------------


def test_set_writes_an_absolute_quantity(logged_in, shopify):
    shopify.set(VARIANT, qty=5)
    res = logged_in.post(
        "/dashboard/api/shopify/inventory/set",
        json={"updates": [{"inventory_item_id": f"gid://shopify/InventoryItem/{VARIANT}", "quantity": 12}]},
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True, "updated": 1}
    # Absolute, not additive: 12 means 12, whatever was on the shelf before.
    assert shopify.qty(VARIANT) == 12


def test_set_batches_several_variants_in_one_call(logged_in, shopify):
    skus = list(shopify.shelf)[:3]
    res = logged_in.post(
        "/dashboard/api/shopify/inventory/set",
        json={"updates": [
            {"inventory_item_id": f"gid://shopify/InventoryItem/{sku}", "quantity": 7} for sku in skus
        ]},
    )
    assert res.status_code == 200
    assert all(shopify.qty(sku) == 7 for sku in skus)


@pytest.mark.parametrize("bad", [
    {"updates": []},
    {"updates": [{"quantity": 3}]},
    {"updates": [{"inventory_item_id": "x", "quantity": -1}]},
    {"updates": [{"inventory_item_id": "x", "quantity": "3"}]},
    {"updates": [{"inventory_item_id": "x", "quantity": True}]},
])
def test_set_refuses_bad_input(logged_in, shopify, bad):
    res = logged_in.post("/dashboard/api/shopify/inventory/set", json=bad)
    assert res.status_code == 400
    assert res.json()["error"] == "bad_arguments"


def test_set_requires_login(client):
    res = client.post("/dashboard/api/shopify/inventory/set", json={"updates": []})
    assert res.status_code == 401
