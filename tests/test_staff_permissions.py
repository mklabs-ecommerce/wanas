"""Who may open which part of the dashboard.

Two things are being pinned here, and the second matters more than the first.

The first is the permission model itself (`domain/services/staff_admin.py`),
including its one surprising rule: an account created before permissions
existed has NULL for both columns and must keep full access, because the
alternative is a deploy that locks the owner out of the screen that hands
permissions out.

The second is that the *endpoints* enforce it. Hiding a nav item in
`dashboard.html` is a courtesy; the route behind it is one `fetch` away. So
every test below that asserts a refusal asserts it against the HTTP call, not
against a helper -- a permission system that only hides buttons passes a UI
test and protects nothing.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from dashboard import (
    collections_api,
    customers_api,
    inbox_api,
    insights_api,
    inventory_api,
    queue_api,
    settings_api,
    shopify_api,
    staff_api,
    stats_api,
    web as dashboard,
)
from domain.services import auth, staff_admin

SECRET = "test-dashboard-secret"
PASSWORD = "correct horse battery"


@pytest.fixture()
def configured(monkeypatch):
    patched = dataclasses.replace(settings, dashboard_session_secret=SECRET)
    monkeypatch.setattr(dashboard, "settings", patched)
    monkeypatch.setattr(auth, "settings", patched)


@pytest.fixture()
def client(configured):
    app = FastAPI()
    for router in (
        dashboard.router,
        shopify_api.router,
        collections_api.router,
        customers_api.router,
        stats_api.router,
        insights_api.router,
        queue_api.router,
        settings_api.router,
        inventory_api.router,
        inbox_api.router,
        staff_api.router,
    ):
        app.include_router(router)
    return TestClient(app)


def _login(client, username, password=PASSWORD):
    res = client.post("/dashboard/api/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return client


@pytest.fixture()
def owner(seeded):
    person = auth.create_staff(seeded, "sara", PASSWORD)
    seeded.commit()
    return person


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------


def test_an_account_from_before_permissions_existed_keeps_full_access(seeded):
    """`auth.create_staff` leaves both new columns NULL, exactly as every row
    already in production has them. That must read as "everything"."""
    person = auth.create_staff(seeded, "amira", PASSWORD)
    assert person.role is None and person.permissions is None
    assert staff_admin.is_owner(person)
    assert set(staff_admin.permission_keys(person)) == set(staff_admin.PERMISSION_KEYS)


def test_an_owner_is_never_scoped_by_a_permission_list(seeded):
    person = staff_admin.create(seeded, "owner2", PASSWORD, role="owner", permissions=["inbox"])
    assert person.permissions is None
    assert staff_admin.has_permission(person, "settings")


def test_a_staff_account_only_gets_what_was_ticked(seeded):
    person = staff_admin.create(seeded, "omar", PASSWORD, role="staff", permissions=["inbox", "orders"])
    assert staff_admin.has_permission(person, "inbox")
    assert not staff_admin.has_permission(person, "settings")
    assert not staff_admin.has_permission(person, "manage_staff")


def test_ticking_nothing_is_stored_as_empty_not_null(seeded):
    """`[]` means "deliberately nothing"; NULL means "never scoped". Collapsing
    the two would silently promote a locked-down account to full access."""
    person = staff_admin.create(seeded, "nobody", PASSWORD, role="staff", permissions=[])
    assert person.permissions == []
    assert staff_admin.permission_keys(person) == ()


def test_an_unknown_permission_is_refused(seeded):
    with pytest.raises(ValueError):
        staff_admin.create(seeded, "typo", PASSWORD, role="staff", permissions=["orderz"])


def test_an_unknown_role_is_refused(seeded):
    with pytest.raises(ValueError):
        staff_admin.create(seeded, "wat", PASSWORD, role="superuser")


def test_promoting_to_owner_clears_the_stored_permission_list(seeded):
    person = staff_admin.create(seeded, "omar", PASSWORD, role="staff", permissions=["inbox"])
    staff_admin.update(seeded, person, role="owner")
    assert person.permissions is None


# --------------------------------------------------------------------------
# enforcement at the endpoints
# --------------------------------------------------------------------------


def test_a_scoped_account_is_refused_the_sections_it_lacks(client, seeded):
    staff_admin.create(seeded, "omar", PASSWORD, role="staff", permissions=["inbox"])
    seeded.commit()
    _login(client, "omar")

    # Granted.
    assert client.get("/dashboard/api/conversations").status_code == 200
    # Not granted -- and 403, not 401: they are logged in, just not allowed.
    for path in (
        "/dashboard/api/shopify/orders",
        "/dashboard/api/shopify/products",
        "/dashboard/api/shopify/customers",
        "/dashboard/api/customers",
        "/dashboard/api/stats?days=7",
        "/dashboard/api/insights?days=7",
        "/dashboard/api/queue",
        "/dashboard/api/settings/flags",
        "/dashboard/api/shopify/inventory",
        "/dashboard/api/shopify/collections",
        "/dashboard/api/staff",
    ):
        res = client.get(path)
        assert res.status_code == 403, f"{path} -> {res.status_code}"
        assert res.json()["error"] == "forbidden"


def test_a_refusal_is_401_when_nobody_is_logged_in(client):
    assert client.get("/dashboard/api/staff").status_code == 401


def test_a_write_route_is_guarded_too_not_only_the_read(client, seeded, cairo_rate):
    """The list endpoint being hidden is not the control -- the POST is."""
    staff_admin.create(seeded, "omar", PASSWORD, role="staff", permissions=["inbox"])
    seeded.commit()
    _login(client, "omar")

    res = client.post("/dashboard/api/settings/flags/voice_notes_enabled", json={"value": False})
    assert res.status_code == 403
    res = client.post("/dashboard/api/staff", json={"username": "x", "password": "12345678"})
    assert res.status_code == 403
    # collections_api guards through its own `_refused` helper rather than the
    # shape every other router uses -- so its POST gets its own assertion.
    res = client.post("/dashboard/api/shopify/collections", json={"title": "New"})
    assert res.status_code == 403


def test_an_owner_reaches_everything(client, owner):
    _login(client, "sara")
    assert client.get("/dashboard/api/staff").status_code == 200
    assert client.get("/dashboard/api/settings/flags").status_code == 200


def test_me_reports_what_this_account_can_reach(client, seeded):
    staff_admin.create(seeded, "omar", PASSWORD, role="staff", permissions=["inbox", "orders"])
    seeded.commit()
    _login(client, "omar")
    body = client.get("/dashboard/api/me").json()
    assert body["role"] == "staff"
    assert set(body["permissions"]) == {"inbox", "orders"}


def test_login_works_for_an_account_with_no_permissions_at_all(client, seeded):
    """Otherwise there is no way to tell someone their access was removed --
    they would just see a broken login."""
    staff_admin.create(seeded, "nobody", PASSWORD, role="staff", permissions=[])
    seeded.commit()
    _login(client, "nobody")
    body = client.get("/dashboard/api/me").json()
    assert body["permissions"] == []


# --------------------------------------------------------------------------
# the Team section
# --------------------------------------------------------------------------


def test_the_owner_can_add_a_scoped_member(client, owner):
    _login(client, "sara")
    res = client.post(
        "/dashboard/api/staff",
        json={"username": "omar", "password": "another good one", "role": "staff",
              "permissions": ["inbox", "orders"]},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["role"] == "staff"
    assert body["permissions"] == ["inbox", "orders"]


def test_a_new_member_can_log_in_with_the_password_that_was_set(client, owner):
    _login(client, "sara")
    client.post(
        "/dashboard/api/staff",
        json={"username": "omar", "password": "another good one", "role": "staff",
              "permissions": ["inbox"]},
    )
    client.post("/dashboard/api/logout")
    _login(client, "omar", "another good one")


def test_a_short_password_is_refused(client, owner):
    _login(client, "sara")
    res = client.post("/dashboard/api/staff", json={"username": "omar", "password": "short"})
    assert res.status_code == 400


def test_a_duplicate_username_is_refused(client, owner):
    _login(client, "sara")
    res = client.post("/dashboard/api/staff", json={"username": "sara", "password": "another good one"})
    assert res.status_code == 400


def test_permissions_can_be_changed_on_an_existing_member(client, owner, seeded):
    person = staff_admin.create(seeded, "omar", PASSWORD, role="staff", permissions=["inbox"])
    seeded.commit()
    _login(client, "sara")
    res = client.post(f"/dashboard/api/staff/{person.staff_id}", json={"permissions": ["orders"]})
    assert res.status_code == 200
    assert res.json()["permissions"] == ["orders"]


def test_a_deactivated_member_cannot_log_in(client, owner, seeded):
    person = staff_admin.create(seeded, "omar", PASSWORD, role="staff", permissions=["inbox"])
    seeded.commit()
    _login(client, "sara")
    assert client.post(f"/dashboard/api/staff/{person.staff_id}", json={"is_active": False}).status_code == 200
    client.post("/dashboard/api/logout")
    res = client.post("/dashboard/api/login", json={"username": "omar", "password": PASSWORD})
    assert res.status_code == 401


def test_nobody_can_edit_their_own_role_or_permissions(client, owner):
    _login(client, "sara")
    res = client.post(f"/dashboard/api/staff/{owner.staff_id}", json={"permissions": []})
    assert res.status_code == 409
    assert res.json()["error"] == "self_edit_refused"


def test_the_last_owner_cannot_be_demoted(client, seeded):
    """A shop with no owner has nobody who can hand permissions out, and the
    only way back is a shell on the database."""
    first = staff_admin.create(seeded, "sara", PASSWORD, role="owner")
    second = staff_admin.create(seeded, "amira", PASSWORD, role="owner")
    seeded.commit()
    _login(client, "sara")

    # Two owners: demoting the other one is allowed.
    res = client.post(f"/dashboard/api/staff/{second.staff_id}", json={"role": "staff"})
    assert res.status_code == 200, res.text

    # `first` is now the only owner left. Demoting them is what would leave
    # the shop with none -- refused, even though a self-edit would be refused
    # first, so it is asked for from the account that still may ask.
    client.post("/dashboard/api/logout")
    staff_admin.update(seeded, second, role="owner")
    seeded.commit()
    _login(client, "amira")
    staff_admin.update(seeded, second, role="staff", permissions=["manage_staff"])
    seeded.commit()

    res = client.post(f"/dashboard/api/staff/{first.staff_id}", json={"role": "staff"})
    assert res.status_code == 409
    assert res.json()["error"] == "last_owner"


def test_the_last_owner_cannot_be_deactivated(client, seeded):
    first = auth.create_staff(seeded, "sara", PASSWORD)
    second = staff_admin.create(seeded, "amira", PASSWORD, role="owner")
    seeded.commit()
    _login(client, "amira")

    # `first` is a grandfathered account: NULL role, read as owner. With two
    # owners, deactivating one is fine.
    assert client.post(f"/dashboard/api/staff/{first.staff_id}", json={"is_active": False}).status_code == 200
    # `second` is now the only active owner, and is the caller.
    res = client.post(f"/dashboard/api/staff/{second.staff_id}", json={"is_active": False})
    assert res.status_code == 409
    assert res.json()["error"] == "last_owner"


def test_the_team_list_carries_the_permission_catalog(client, owner):
    _login(client, "sara")
    body = client.get("/dashboard/api/staff").json()
    assert {p["key"] for p in body["permissions"]} == set(staff_admin.PERMISSION_KEYS)
    assert all(p["label_ar"] for p in body["permissions"])
    assert body["me"] == owner.staff_id
