"""The Team section: who can log in, and which parts of the dashboard they see.

A sibling router under the same staff-cookie guard as the rest of the
dashboard (see `web.py`'s docstring for why this package grew files instead of
one growing file), gated on the `manage_staff` permission -- which, per
`domain/services/staff_admin.py`, every owner and every pre-permissions
account already has.

Two refusals worth naming, both of them "you cannot lock the shop out of
itself": the last active owner can neither be demoted nor deactivated, and an
account cannot edit its own role or permissions. The second is not paranoia
about a malicious owner; it is the ordinary case of someone ticking their own
boxes down to nothing and then having no way to tick them back.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Cookie
from fastapi.responses import JSONResponse

from dashboard.guard import require_permission
from domain.db import session_scope
from domain.models import Staff
from domain.services import staff_admin

router = APIRouter(prefix="/dashboard/api/staff", tags=["dashboard-staff"])

PERMISSION = "manage_staff"


def _catalog() -> list[dict]:
    return [
        {
            "key": p.key,
            "label_ar": p.label_ar,
            "description_ar": p.description_ar,
            # See `settings_api._flag_payload`: server-authored strings carry
            # both languages rather than relying on the page's dictionary.
            "label_en": p.label_en,
            "description_en": p.description_en,
        }
        for p in staff_admin.PERMISSIONS
    ]


@router.get("")
def list_team(wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        staff, refused = require_permission(db, wanas_staff, PERMISSION)
        if refused is not None:
            return refused
        result = {
            "staff": [staff_admin.summary(row) for row in staff_admin.list_staff(db)],
            "permissions": _catalog(),
            "roles": list(staff_admin.ROLES),
            "me": staff.staff_id,
        }
    return JSONResponse(result)


@router.post("")
def create_member(
    payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        _, refused = require_permission(db, wanas_staff, PERMISSION)
        if refused is not None:
            return refused
        try:
            member = staff_admin.create(
                db,
                (payload.get("username") or "").strip(),
                payload.get("password") or "",
                role=payload.get("role") or staff_admin.STAFF_ROLE,
                permissions=payload.get("permissions"),
            )
        except ValueError as exc:
            return JSONResponse({"error": "bad_arguments", "detail": str(exc)}, status_code=400)
        result = staff_admin.summary(member)
    return JSONResponse(result, status_code=201)


@router.post("/{staff_id}")
def update_member(
    staff_id: int, payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        me, refused = require_permission(db, wanas_staff, PERMISSION)
        if refused is not None:
            return refused

        member = db.get(Staff, staff_id)
        if member is None:
            return JSONResponse({"error": "staff_not_found"}, status_code=404)

        role = payload.get("role")
        permissions = payload.get("permissions")
        is_active = payload.get("is_active")

        if member.staff_id == me.staff_id and (role is not None or permissions is not None):
            detail = "you cannot change your own role or permissions"
            return JSONResponse({"error": "self_edit_refused", "detail": detail}, status_code=409)

        losing_owner = staff_admin.is_owner(member) and (
            (role is not None and role != staff_admin.OWNER_ROLE) or is_active is False
        )
        if losing_owner and staff_admin.owner_count(db, exclude=member.staff_id) == 0:
            detail = "the last owner cannot be demoted or deactivated"
            return JSONResponse({"error": "last_owner", "detail": detail}, status_code=409)

        try:
            staff_admin.update(
                db,
                member,
                role=role,
                permissions=permissions,
                is_active=is_active,
                password=(payload.get("password") or None),
            )
        except ValueError as exc:
            return JSONResponse({"error": "bad_arguments", "detail": str(exc)}, status_code=400)
        result = staff_admin.summary(member)
    return JSONResponse(result)
