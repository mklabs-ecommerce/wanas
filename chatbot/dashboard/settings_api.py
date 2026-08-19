"""Automations panel: the feature toggles and a read-only system-status view.

Sits next to `web.py` rather than in it -- see that module's docstring for
why the dashboard grew sibling files instead of one growing file. Every route
here uses the same staff-cookie guard `web.py`'s own routes use
(`chatbot.dashboard.guard`).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Cookie
from fastapi.responses import JSONResponse

from backend.config import settings
from backend.db import session_scope
from backend.models import Staff
from backend.services import runtime_flags, test_numbers
from chatbot.dashboard.guard import staff_for, unauthenticated

router = APIRouter(prefix="/dashboard/api/settings", tags=["dashboard-settings"])


def _flag_payload(session, flag, row) -> dict:
    updated_by = None
    if row is not None and row.updated_by is not None:
        staff = session.get(Staff, row.updated_by)
        updated_by = staff.username if staff else None
    return {
        "key": flag.key,
        "label_ar": flag.label_ar,
        "description_ar": flag.description_ar,
        "value": row.value if row is not None else getattr(settings, flag.key),
        "overridden": row is not None,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
        "updated_by": updated_by,
    }


@router.get("/flags")
def list_flags(wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        rows = runtime_flags.get_all(db)
        flags = [_flag_payload(db, flag, rows.get(flag.key)) for flag in runtime_flags.KNOWN_FLAGS]
    return JSONResponse({"flags": flags})


@router.post("/flags/{key}")
def set_flag(
    key: str, payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        staff = staff_for(db, wanas_staff)
        if staff is None:
            return unauthenticated()

        if "value" not in payload or not isinstance(payload["value"], bool):
            detail = "value must be a boolean"
            return JSONResponse({"error": "bad_arguments", "detail": detail}, status_code=400)
        try:
            row = runtime_flags.set(db, key, payload["value"], staff.staff_id)
        except ValueError:
            return JSONResponse({"error": "unknown_flag", "key": key}, status_code=404)
        flag = next(f for f in runtime_flags.KNOWN_FLAGS if f.key == key)
        result = _flag_payload(db, flag, row)
    return JSONResponse(result)


def _number_payload(session, row) -> dict:
    staff = session.get(Staff, row.added_by) if row.added_by is not None else None
    return {
        "phone": row.phone,
        "note": row.note,
        "added_at": row.added_at.isoformat() if row.added_at else None,
        "added_by": staff.username if staff else None,
    }


@router.get("/test-numbers")
def list_test_numbers(wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """Numbers marked as staff testing the bot -- excluded from the
    Statistics page's totals. See `backend/services/test_numbers.py`."""
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        rows = test_numbers.list_numbers(db)
        numbers = [_number_payload(db, row) for row in rows]
    return JSONResponse({"numbers": numbers})


@router.post("/test-numbers")
def add_test_number(
    payload: dict = Body(...), wanas_staff: str | None = Cookie(default=None)
) -> JSONResponse:
    with session_scope() as db:
        staff = staff_for(db, wanas_staff)
        if staff is None:
            return unauthenticated()
        phone = (payload.get("phone") or "").strip()
        if not any(ch.isdigit() for ch in phone):
            return JSONResponse({"error": "bad_arguments", "detail": "phone is required"}, status_code=400)
        note = (payload.get("note") or "").strip() or None
        row = test_numbers.add(db, phone, note=note, staff_id=staff.staff_id)
        result = _number_payload(db, row)
    return JSONResponse(result, status_code=201)


@router.delete("/test-numbers/{phone}")
def remove_test_number(phone: str, wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()
        removed = test_numbers.remove(db, phone)
    if not removed:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.get("/status")
def system_status(wanas_staff: str | None = Cookie(default=None)) -> JSONResponse:
    """The same booleans `/health` reports, behind the staff login rather than
    the open endpoint -- useful from inside the dashboard without a second tab."""
    with session_scope() as db:
        if staff_for(db, wanas_staff) is None:
            return unauthenticated()

    return JSONResponse(
        {
            "llm_provider": settings.llm_provider,
            "llm_key_set": bool(settings.llm_api_key),
            "whatsapp_configured": settings.whatsapp_configured,
            "shopify_configured": settings.shopify_configured,
            "shopify_webhooks_configured": settings.shopify_webhooks_configured,
            "dashboard_configured": settings.dashboard_configured,
        }
    )
