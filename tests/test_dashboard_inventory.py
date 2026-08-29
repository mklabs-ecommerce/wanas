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


# --------------------------------------------------------------------------
# what actually goes over the wire
# --------------------------------------------------------------------------
#
# The fake shelf stands in for `shopify_set_inventory` itself, so nothing above
# this line ever looks at the mutation's variables. That is exactly where a
# saved quantity died in production: the input carried `ignoreCompareQuantity`,
# a field Shopify has removed from `InventorySetQuantitiesInput`, and an
# unknown field fails the whole document before the shelf is ever touched.


class _Recorder:
    """A client that answers like Shopify and keeps the variables it was sent."""

    version = "2026-07"

    def __init__(self, response=None, on_shelf=0):
        self.calls = []
        self.on_shelf = on_shelf
        self.response = response or {"inventorySetQuantities": {"userErrors": []}}
        #: Set to answer the writes yourself; the reads are answered here
        #: either way.
        self.handler = None

    def __call__(self, query, variables=None):
        if "inventoryLevel" in query:
            return {
                "nodes": [
                    {
                        "id": item_id,
                        "inventoryLevel": {
                            "quantities": [{"name": "available", "quantity": self.on_shelf}]
                        },
                    }
                    for item_id in variables["ids"]
                ]
            }
        self.calls.append(variables)
        if self.handler is not None:
            return self.handler(len(self.calls))
        return self.response


@pytest.fixture()
def recorder(monkeypatch):
    from integrations.shopify import admin_products, inventory as shopify_inventory

    rec = _Recorder()
    monkeypatch.setattr(admin_products, "get_admin_client", lambda: rec)
    monkeypatch.setattr(shopify_inventory, "location_id", lambda: "gid://shopify/Location/1")
    return rec


@pytest.mark.no_shopify
def test_the_stock_write_sends_only_fields_shopify_still_has(recorder):
    from integrations.shopify.admin_products import shopify_set_inventory

    recorder.on_shelf = 2
    shopify_set_inventory([{"inventory_item_id": "gid://shopify/InventoryItem/9", "quantity": 7}])

    sent = recorder.calls[0]["input"]
    assert "ignoreCompareQuantity" not in sent
    assert sent["quantities"] == [
        {
            "inventoryItemId": "gid://shopify/InventoryItem/9",
            "locationId": "gid://shopify/Location/1",
            "quantity": 7,
            # Not skipped, satisfied: Shopify requires the compare, so the
            # write states the number it just read rather than the number the
            # staff member is replacing it with.
            "changeFromQuantity": 2,
        }
    ]
    assert recorder.calls[0]["key"], "the mutation is refused without one"


@pytest.mark.no_shopify
def test_a_shelf_that_moved_mid_correction_is_re_read_and_re_applied(recorder):
    """Staff counted seven on the shelf. Somebody sold one while the form was
    open. Seven is still the right answer -- the correction must land, not
    bounce back as an error."""
    from integrations.shopify.admin_products import shopify_set_inventory

    recorder.on_shelf = 2
    stale = {
        "inventorySetQuantities": {
            "userErrors": [
                {
                    "field": None,
                    "message": "The changeFromQuantity argument no longer matches the persisted quantity.",
                }
            ]
        }
    }

    def someone_sold_one(attempt):
        if attempt == 1:
            recorder.on_shelf = 1
            return stale
        return {"inventorySetQuantities": {"userErrors": []}}

    recorder.handler = someone_sold_one
    shopify_set_inventory([{"inventory_item_id": "gid://shopify/InventoryItem/9", "quantity": 7}])

    assert len(recorder.calls) == 2
    assert recorder.calls[0]["input"]["quantities"][0]["changeFromQuantity"] == 2
    assert recorder.calls[1]["input"]["quantities"][0]["changeFromQuantity"] == 1
    assert recorder.calls[1]["input"]["quantities"][0]["quantity"] == 7
    assert recorder.calls[0]["key"] != recorder.calls[1]["key"]


@pytest.mark.no_shopify
def test_an_api_version_that_cannot_compare_is_refused_before_the_write(recorder):
    from integrations.shopify.admin_products import shopify_set_inventory
    from integrations.shopify.client import ShopifyUnavailable

    recorder.version = "2025-01"

    with pytest.raises(ShopifyUnavailable, match="SHOPIFY_API_VERSION"):
        shopify_set_inventory(
            [{"inventory_item_id": "gid://shopify/InventoryItem/9", "quantity": 7}]
        )

    assert recorder.calls == []


@pytest.mark.no_shopify
def test_a_refused_stock_write_is_raised_not_swallowed(recorder):
    """Absent this, Shopify could reject the save and the dashboard would
    still say it worked -- the numbers on screen would be a wish."""
    from integrations.shopify.admin_products import ProductRejected, shopify_set_inventory

    recorder.response = {
        "inventorySetQuantities": {"userErrors": [{"field": None, "message": "no such item"}]}
    }

    with pytest.raises(ProductRejected, match="no such item"):
        shopify_set_inventory([{"inventory_item_id": "gid://shopify/InventoryItem/9", "quantity": 7}])
