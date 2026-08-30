"""Store-wide statistics: the pure aggregation math (`summarize`), and the
paginated Shopify read + the dashboard endpoint on top of it.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import stats_api, web as dashboard
from domain.services import (
    auth,
    carts,
    dashboard_stats,
    orders,
)

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


# --------------------------------------------------------------------------
# the Analytics page's toggles, and the two series added with them
# --------------------------------------------------------------------------


def test_payment_filter_narrows_every_number_not_just_a_chart():
    """A page that filtered the chart but left the KPI above it counting
    everything would be worse than no toggle at all."""
    orders_in = [
        {**_order(total=100, customer_phone="1"), "payment_method": "cod"},
        {**_order(total=300, customer_phone="2"), "payment_method": "online"},
    ]
    cod = dashboard_stats.summarize([dict(o) for o in orders_in], payment="cod")
    assert cod["order_count"] == 1
    assert cod["revenue"] == "100"
    assert cod["customer_count"] == 1

    online = dashboard_stats.summarize([dict(o) for o in orders_in], payment="online")
    assert online["revenue"] == "300"

    both = dashboard_stats.summarize([dict(o) for o in orders_in], payment="all")
    assert both["revenue"] == "400"


def test_an_order_with_no_payment_method_lands_in_unknown():
    result = dashboard_stats.summarize([_order(total=100)])
    assert result["payment_breakdown"] == {"unknown": 1}


def test_channel_comes_from_the_map_first():
    a, b = _order(total=100, customer_phone="1"), _order(total=200, customer_phone="2")
    result = dashboard_stats.summarize(
        [a, b], channel_by_order_id={a["id"]: "instagram_dm"}
    )
    assert result["channel_breakdown"] == {"instagram_dm": 1, "web": 1}
    assert result["bot_order_count"] == 1


def test_an_order_with_no_local_row_is_read_off_shopify_not_called_web():
    """The Analytics bug. Only bot orders reach Postgres, and only since
    `shopify_order_id` existed -- defaulting the rest to "web" reported the
    bot's whole earlier history as website sales on the one page that exists
    to say where sales came from."""
    order = {**_order(total=100), "channel_hint": "instagram_dm"}

    result = dashboard_stats.summarize([order])

    assert result["channel_breakdown"] == {"instagram_dm": 1}
    assert result["bot_order_count"] == 1


def test_an_order_shopify_calls_the_website_is_still_the_website():
    result = dashboard_stats.summarize([{**_order(total=100), "channel_hint": "web"}])

    assert result["channel_breakdown"] == {"web": 1}


def test_the_channel_filter_finds_an_order_with_no_local_row():
    a = {**_order(total=100, customer_phone="1"), "channel_hint": "instagram_dm"}
    b = {**_order(total=200, customer_phone="2"), "channel_hint": "whatsapp"}

    result = dashboard_stats.summarize([a, b], channel="instagram_dm")

    assert result["order_count"] == 1
    assert result["revenue"] == "100"


def test_channel_filter_narrows_the_totals():
    a, b = _order(total=100, customer_phone="1"), _order(total=200, customer_phone="2")
    result = dashboard_stats.summarize(
        [a, b], channel_by_order_id={a["id"]: "whatsapp"}, channel="whatsapp"
    )
    assert result["order_count"] == 1
    assert result["revenue"] == "100"


def test_orders_by_governorate_counts_and_ranks():
    orders_in = [
        _order(total=100, customer_phone="1"),
        _order(total=200, customer_phone="2"),
        {**_order(total=50, customer_phone="3"), "governorate": "Giza"},
    ]
    rows = dashboard_stats.summarize(orders_in)["orders_by_governorate"]
    assert rows[0] == {"governorate": "Cairo", "orders": 2, "revenue": "300"}
    assert rows[1] == {"governorate": "Giza", "orders": 1, "revenue": "50"}


def test_an_order_with_no_governorate_is_bucketed_not_dropped():
    """Otherwise the chart's slices stop adding up to the order count printed
    directly above them, and nothing on screen says why."""
    orders_in = [
        _order(total=100, customer_phone="1"),
        {**_order(total=50, customer_phone="2"), "governorate": None},
    ]
    result = dashboard_stats.summarize(orders_in)
    rows = result["orders_by_governorate"]
    assert sum(r["orders"] for r in rows) == result["order_count"]
    assert any(r["governorate"] == "" for r in rows)


def test_customer_kind_breakdown_keeps_unknown_separate_from_new():
    orders_in = [
        {**_order(total=100, customer_phone="1"), "customer_kind": "new"},
        {**_order(total=100, customer_phone="2"), "customer_kind": "returning"},
        _order(total=100, customer_phone="3"),  # no customer record at all
    ]
    breakdown = dashboard_stats.summarize(orders_in)["customer_kind_breakdown"]
    assert breakdown == {"new": 1, "returning": 1, "unknown": 1}


def test_the_range_does_not_decide_who_is_a_new_customer(monkeypatch):
    """Whether an order was its buyer's first is a fact about the shop, not
    about the window on screen: inside a 30-day range every order looks like a
    first one. `fetch_orders_in_range` relabels against the whole history."""
    from integrations.shopify import admin_orders as shopify_admin_orders

    first = {**_order(total=100, customer_phone="1"), "id": "gid://shopify/Order/A",
             "customer_gid": "gid://shopify/Customer/1"}
    second = {**_order(total=100, customer_phone="1"), "id": "gid://shopify/Order/B",
              "customer_gid": "gid://shopify/Customer/1"}
    monkeypatch.setattr(
        shopify_admin_orders, "list_orders",
        lambda **_: {"orders": [second], "has_next_page": False, "end_cursor": None},
    )
    monkeypatch.setattr(
        shopify_admin_orders, "cached_first_order_ids", lambda: frozenset({first["id"]})
    )
    fetched, _truncated = dashboard_stats.fetch_orders_in_range(
        dashboard_stats.range_for_days(30)
    )
    assert [o["customer_kind"] for o in fetched] == ["returning"]


def test_stats_endpoint_rejects_an_unknown_payment_or_channel(logged_in):
    assert logged_in.get("/dashboard/api/stats?days=30&payment=bitcoin").status_code == 400
    assert logged_in.get("/dashboard/api/stats?days=30&channel=telegram").status_code == 400


def test_stats_endpoint_attributes_a_bot_order_to_its_channel(
    logged_in, cairo_rate, seeded, shopify
):
    carts.add(seeded, "instagram_dm", "ig-777", VARIANT, 1)
    result = orders.place_order(
        seeded, channel="instagram_dm", external_id="ig-777", customer_name="IG Buyer",
        governorate="Cairo", address="1 St", contact_phone="01000000111",
    )
    assert "error" not in result, result
    seeded.commit()

    body = logged_in.get("/dashboard/api/stats?days=30").json()
    assert body["channel_breakdown"].get("instagram_dm") == 1

    # And the toggle finds it under Instagram, not under WhatsApp -- which is
    # what the `whatsapp` tag on every bot order would have said.
    ig = logged_in.get("/dashboard/api/stats?days=30&channel=instagram_dm").json()
    assert ig["order_count"] == 1
    wa = logged_in.get("/dashboard/api/stats?days=30&channel=whatsapp").json()
    assert wa["order_count"] == 0
