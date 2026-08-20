"""Store-wide statistics: the pure aggregation math (`summarize`), and the
paginated Shopify read + the dashboard endpoint on top of it.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings
from backend.services import auth, carts, dashboard_stats, orders
from dashboard import stats_api
from dashboard import web as dashboard

SECRET = "test-dashboard-secret"
VARIANT = "wanas-hoodie-s-olive"


def _order(*, total, cancelled=False, fulfillment_status="UNFULFILLED", source="chatbot",
           customer_phone="0100", created_at="2026-01-05", line_items=None):
    return {
        "id": f"gid://shopify/Order/{total}-{customer_phone}",
        "name": "#1",
        "created_at": created_at,
        "financial_status": "PENDING",
        "fulfillment_status": fulfillment_status,
        "cancelled": cancelled,
        "tags": [],
        "customer_name": "Someone",
        "customer_phone": customer_phone,
        "governorate": "Cairo",
        "total": str(total),
        "line_items": line_items or [{"title": "WANAS Hoodie", "quantity": 1, "sku": VARIANT}],
        "source": source,
    }


# --------------------------------------------------------------------------
# summarize() -- pure math
# --------------------------------------------------------------------------


def test_revenue_excludes_cancelled_orders():
    orders_in = [_order(total=100), _order(total=50, cancelled=True)]
    result = dashboard_stats.summarize(orders_in)
    assert result["revenue"] == "100"
    assert result["order_count"] == 1
    assert result["cancelled_count"] == 1


def test_average_order_value():
    orders_in = [_order(total=100, customer_phone="1"), _order(total=200, customer_phone="2")]
    result = dashboard_stats.summarize(orders_in)
    assert Decimal(result["average_order_value"]) == Decimal("150")


def test_average_order_value_is_zero_with_no_orders():
    assert dashboard_stats.summarize([])["average_order_value"] == "0"


def test_distinct_customers_are_deduplicated_by_phone():
    orders_in = [
        _order(total=100, customer_phone="0100"),
        _order(total=50, customer_phone="0100"),
        _order(total=50, customer_phone="0200"),
    ]
    assert dashboard_stats.summarize(orders_in)["customer_count"] == 2


def test_best_sellers_sums_quantity_across_orders():
    orders_in = [
        _order(total=1, line_items=[{"title": "Hoodie", "quantity": 2, "sku": "a"}]),
        _order(total=1, line_items=[{"title": "Hoodie", "quantity": 3, "sku": "a"}]),
        _order(total=1, line_items=[{"title": "Cap", "quantity": 1, "sku": "b"}]),
    ]
    best = dashboard_stats.summarize(orders_in)["best_sellers"]
    assert best[0] == {"title": "Hoodie", "quantity": 5}


def test_cancelled_orders_do_not_count_toward_best_sellers():
    orders_in = [_order(total=1, cancelled=True, line_items=[{"title": "Hoodie", "quantity": 9, "sku": "a"}])]
    assert dashboard_stats.summarize(orders_in)["best_sellers"] == []


def test_status_breakdown_counts_cancelled_separately_from_fulfillment_status():
    orders_in = [
        _order(total=1, fulfillment_status="FULFILLED"),
        _order(total=1, fulfillment_status="UNFULFILLED"),
        _order(total=1, cancelled=True),
    ]
    breakdown = dashboard_stats.summarize(orders_in)["status_breakdown"]
    assert breakdown == {"FULFILLED": 1, "UNFULFILLED": 1, "cancelled": 1}


def test_source_breakdown_distinguishes_bot_from_website():
    orders_in = [_order(total=1, source="chatbot"), _order(total=1, source="website")]
    breakdown = dashboard_stats.summarize(orders_in)["source_breakdown"]
    assert breakdown == {"chatbot": 1, "website": 1}


def test_revenue_by_day_groups_and_sums():
    orders_in = [
        _order(total=100, created_at="2026-01-05T10:00:00Z"),
        _order(total=50, created_at="2026-01-05T18:00:00Z", customer_phone="2"),
        _order(total=30, created_at="2026-01-06T10:00:00Z", customer_phone="3"),
    ]
    series = dashboard_stats.summarize(orders_in)["revenue_by_day"]
    assert series == [
        {"date": "2026-01-05", "revenue": "150", "orders": 2},
        {"date": "2026-01-06", "revenue": "30", "orders": 1},
    ]


# --------------------------------------------------------------------------
# fetch_orders_in_range -- pagination against the fake
# --------------------------------------------------------------------------


def test_fetch_orders_in_range_pages_through_a_fake_shopify(seeded, cairo_rate, shopify):
    for i in range(3):
        carts.add(seeded, "whatsapp", f"20155500{i}", VARIANT, 1)
        result = orders.place_order(
            seeded, channel="whatsapp", external_id=f"20155500{i}", customer_name=f"C{i}",
            governorate="Cairo", address="1 St", contact_phone=f"0100000{i}",
        )
        assert "error" not in result, result
    seeded.commit()

    date_range = dashboard_stats.range_for_days(30)
    fetched, truncated = dashboard_stats.fetch_orders_in_range(date_range)
    assert len(fetched) == 3
    assert truncated is False


def test_fetch_orders_in_range_reports_truncation_when_capped(seeded, cairo_rate, shopify):
    for i in range(3):
        carts.add(seeded, "whatsapp", f"20166600{i}", VARIANT, 1)
        orders.place_order(
            seeded, channel="whatsapp", external_id=f"20166600{i}", customer_name=f"C{i}",
            governorate="Cairo", address="1 St", contact_phone=f"0100000{i}",
        )
    seeded.commit()

    date_range = dashboard_stats.range_for_days(30)
    # The fake returns everything on one page (has_next_page is always
    # False), so max_pages=0 is the only way to force the cap here.
    fetched, truncated = dashboard_stats.fetch_orders_in_range(date_range, max_pages=0)
    assert fetched == []
    assert truncated is True


# --------------------------------------------------------------------------
# the dashboard endpoint
# --------------------------------------------------------------------------


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(stats_api.router)
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


def test_stats_endpoint_requires_login(client):
    assert client.get("/dashboard/api/stats").status_code == 401


def test_stats_endpoint_rejects_an_unlisted_range(logged_in):
    assert logged_in.get("/dashboard/api/stats?days=13").status_code == 400


def test_stats_endpoint_reflects_a_placed_order(logged_in, cairo_rate, seeded, shopify):
    carts.add(seeded, "whatsapp", "201555000999", VARIANT, 1)
    result = orders.place_order(
        seeded, channel="whatsapp", external_id="201555000999", customer_name="Stat Test",
        governorate="Cairo", address="1 St", contact_phone="01000000000",
    )
    assert "error" not in result, result
    seeded.commit()

    res = logged_in.get("/dashboard/api/stats?days=30")
    assert res.status_code == 200
    body = res.json()
    assert body["order_count"] == 1
    assert Decimal(body["revenue"]) > 0


def test_stats_endpoint_reports_an_outage(logged_in, shopify):
    shopify.down = True
    assert logged_in.get("/dashboard/api/stats?days=30").status_code == 503
