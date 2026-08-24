"""Staff-marked "this is me testing" phone numbers: the service layer, the
dashboard settings panel on top of it, and the fact that a marked number's
orders actually disappear from `dashboard_stats.summarize`.

See `backend/services/test_numbers.py`'s module docstring for why this
exists: `dashboard_stats` reads live from Shopify, and a store owner testing
the bot from their own WhatsApp number places real, otherwise-indistinguishable
orders that were inflating every KPI.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import settings_api
from dashboard import web as dashboard
from domain.services import (
    auth,
    dashboard_stats,
    test_numbers,
)

SECRET = "test-dashboard-secret"


# --------------------------------------------------------------------------
# the service layer
# --------------------------------------------------------------------------


def test_a_marked_number_expands_to_every_egyptian_spelling(seeded, staff):
    test_numbers.add(seeded, "01001234567", note=None, staff_id=staff.staff_id)
    variants = test_numbers.all_variants(seeded)
    assert {"01001234567", "201001234567", "1001234567"} <= variants


def test_removing_a_number_drops_it_from_the_list(seeded, staff):
    test_numbers.add(seeded, "01001234567", note="my testing number", staff_id=staff.staff_id)
    assert test_numbers.remove(seeded, "01001234567") is True
    assert test_numbers.list_numbers(seeded) == []


def test_removing_a_number_never_added_reports_nothing_to_remove(seeded):
    assert test_numbers.remove(seeded, "01000000000") is False


def test_adding_the_same_number_twice_updates_rather_than_duplicates(seeded, staff):
    test_numbers.add(seeded, "01001234567", note="first", staff_id=staff.staff_id)
    test_numbers.add(seeded, "01001234567", note="second", staff_id=staff.staff_id)
    rows = test_numbers.list_numbers(seeded)
    assert len(rows) == 1
    assert rows[0].note == "second"


# --------------------------------------------------------------------------
# summarize() actually excludes a marked number's orders
# --------------------------------------------------------------------------


def _order(*, total, customer_phone, cancelled=False):
    return {
        "id": f"gid://shopify/Order/{customer_phone}",
        "name": "#1",
        "created_at": "2026-01-05T10:00:00Z",
        "financial_status": "PENDING",
        "fulfillment_status": "UNFULFILLED",
        "cancelled": cancelled,
        "tags": [],
        "customer_name": "Someone",
        "customer_phone": customer_phone,
        "governorate": "Cairo",
        "total": str(total),
        "line_items": [{"title": "WANAS Hoodie", "quantity": 1, "sku": "wanas-hoodie-s-olive"}],
        "source": "chatbot",
    }


def test_summarize_excludes_orders_from_a_marked_number():
    orders_in = [
        _order(total=100, customer_phone="201001234567"),
        _order(total=9999, customer_phone="201009999999"),
    ]
    result = dashboard_stats.summarize(orders_in, exclude_phones={"201009999999"})
    assert result["revenue"] == "100"
    assert result["order_count"] == 1
    assert result["excluded_test_orders"] == 1


def test_summarize_matches_regardless_of_which_egyptian_spelling_shopify_stored():
    # Staff marked "01009999999"; Shopify happens to have stored the order
    # under the "20"-prefixed international form.
    from domain.services.identities import phone_variants

    orders_in = [_order(total=9999, customer_phone="201009999999")]
    result = dashboard_stats.summarize(orders_in, exclude_phones=set(phone_variants("01009999999")))
    assert result["order_count"] == 0
    assert result["excluded_test_orders"] == 1


def test_with_no_marked_numbers_nothing_is_excluded():
    orders_in = [_order(total=100, customer_phone="201001234567")]
    result = dashboard_stats.summarize(orders_in, exclude_phones=set())
    assert result["order_count"] == 1
    assert result["excluded_test_orders"] == 0


# --------------------------------------------------------------------------
# the date-range boundary is explicit UTC on both ends
# --------------------------------------------------------------------------


def test_date_range_query_uses_explicit_utc_on_both_bounds():
    date_range = dashboard_stats.range_for_days(7)
    query = date_range.as_query()
    assert f"created_at:>={date_range.start.date().isoformat()}T00:00:00Z" in query
    assert f"created_at:<={date_range.end.date().isoformat()}T23:59:59Z" in query


# --------------------------------------------------------------------------
# the dashboard settings panel
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
    app.include_router(settings_api.router)
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


def test_test_numbers_list_requires_login(client):
    assert client.get("/dashboard/api/settings/test-numbers").status_code == 401


def test_adding_a_number_persists_and_attributes_the_staff(logged_in):
    res = logged_in.post(
        "/dashboard/api/settings/test-numbers", json={"phone": "01001234567", "note": "my phone"}
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["phone"] == "01001234567"
    assert body["note"] == "my phone"
    assert body["added_by"] == "sara"

    again = logged_in.get("/dashboard/api/settings/test-numbers")
    assert len(again.json()["numbers"]) == 1


def test_adding_a_blank_phone_is_rejected(logged_in):
    res = logged_in.post("/dashboard/api/settings/test-numbers", json={"phone": "   "})
    assert res.status_code == 400


def test_removing_a_number(logged_in):
    logged_in.post("/dashboard/api/settings/test-numbers", json={"phone": "01001234567"})
    res = logged_in.delete("/dashboard/api/settings/test-numbers/01001234567")
    assert res.status_code == 200
    assert logged_in.get("/dashboard/api/settings/test-numbers").json()["numbers"] == []


def test_removing_a_number_that_was_never_added(logged_in):
    res = logged_in.delete("/dashboard/api/settings/test-numbers/01000000000")
    assert res.status_code == 404
