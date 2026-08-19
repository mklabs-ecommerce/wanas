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
from backend.services import runtime_flags
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
