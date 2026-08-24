"""The one auth check every dashboard route uses, shared rather than
duplicated across `web.py` and the newer `shopify_api.py` / `stats_api.py` /
`settings_api.py` siblings it grew alongside.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from domain.models import Staff
from domain.services import auth


def staff_for(db: Session, token: str | None) -> Staff | None:
    return auth.staff_from_session_token(db, token)


def unauthenticated() -> JSONResponse:
    return JSONResponse({"error": "unauthenticated"}, status_code=401)
