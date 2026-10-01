"""DASHBOARD_DATA_SINCE: the dashboard starts counting at a moment, and the
Shopify orders before it stay in the store but out of every figure."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config.settings as settings_module
from dashboard import shopify_api, stats_api, web as dashboard
from domain.services import auth, staff_admin
from integrations.shopify import admin_orders

SECRET = "test-dashboard-secret"


@pytest.fixture()
def owner(monkeypatch, seeded):
    patched = dataclasses.replace(settings_module.settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)
    app = FastAPI()
    for module in (dashboard, shopify_api, stats_api):
        app.include_router(module.router)
    client = TestClient(app)
    staff_admin.create(seeded, "owner", "owner-password-1", role="owner")
    seeded.commit()
    assert client.post("/dashboard/api/login", json={"username": "owner", "password": "owner-password-1"}).status_code == 200
    return client


def _since(monkeypatch, moment):
    monkeypatch.setattr(
        settings_module, "settings",
        dataclasses.replace(settings_module.settings, dashboard_data_since=moment),
    )


@pytest.fixture()
def two_orders(shopify):
    old = shopify.seed_order(
        customer_name="Old Buyer", phone="201555000111", governorate="Cairo",
        items=[{"variant_id": "1", "quantity": 1, "unit_price": 900, "title": "REHLA Hoodie"}], shipping_fee=60,
    )
    new = shopify.seed_order(
        customer_name="New Buyer", phone="201555000222", governorate="Giza",
        items=[{"variant_id": "1", "quantity": 1, "unit_price": 500, "title": "REHLA Hoodie"}], shipping_fee=60,
    )
    shopify.orders[old]["created_at"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    return old, new


def test_orders_before_day_zero_are_left_out_of_lists_and_figures(owner, two_orders, monkeypatch):
    old, new = two_orders
    before = owner.get("/dashboard/api/stats?days=30").json()
    assert before["order_count"] == 2

    _since(monkeypatch, datetime.now(UTC) - timedelta(days=1))

    listed = {o["id"] for o in owner.get("/dashboard/api/shopify/orders").json()["orders"]}
    assert listed == {new}
    stats = owner.get("/dashboard/api/stats?days=30").json()
    assert stats["order_count"] == 1
    assert Decimal(stats["sales"]["gross_sales"]) == Decimal("500")
    # The store still has it: an old order opens by id, it just is not counted.
    assert owner.get(f"/dashboard/api/shopify/orders/{old}").status_code == 200


def test_unset_counts_everything(two_orders, monkeypatch):
    _since(monkeypatch, None)
    assert admin_orders.scoped_query("x") == "x"
    assert admin_orders.counts_toward_dashboard({"created_at": "2001-01-01T00:00:00Z"})


def test_the_setting_reads_iso_and_refuses_garbage(monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_SINCE", "2026-10-01T17:00:00Z")
    assert settings_module._datetime("DASHBOARD_DATA_SINCE") == datetime(2026, 10, 1, 17, tzinfo=UTC)
    monkeypatch.setenv("DASHBOARD_DATA_SINCE", "2026-10-01T17:00:00")
    assert settings_module._datetime("DASHBOARD_DATA_SINCE").tzinfo is UTC
    monkeypatch.setenv("DASHBOARD_DATA_SINCE", "yesterday")
    assert settings_module._datetime("DASHBOARD_DATA_SINCE") is None
    _since(monkeypatch, datetime(2026, 10, 1, 17, tzinfo=UTC))
    assert admin_orders.scoped_query("status:open") == "(status:open) AND created_at:>=2026-10-01T17:00:00Z"
