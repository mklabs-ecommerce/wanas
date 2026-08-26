"""The one auth check every dashboard route uses, shared rather than
duplicated across `web.py` and the newer `shopify_api.py` / `stats_api.py` /
`settings_api.py` siblings it grew alongside.

`staff_for` answers "is anyone logged in". `require_permission` answers "may
*this* account open this section" -- and it is the only thing that actually
answers it. The sidebar hides nav items an account has no permission for, but
that is a courtesy, not a control: the endpoint behind a hidden button is one
`fetch` away. See `domain/services/staff_admin.py` for why a NULL role reads
as full access rather than none.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from domain.models import Staff
from domain.services import auth, staff_admin


def staff_for(db: Session, token: str | None) -> Staff | None:
    return auth.staff_from_session_token(db, token)


def unauthenticated() -> JSONResponse:
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


def forbidden(permission: str) -> JSONResponse:
    return JSONResponse(
        {"error": "forbidden", "permission": permission},
        status_code=403,
    )


def require_permission(
    db: Session, token: str | None, permission: str
) -> tuple[Staff | None, JSONResponse | None]:
    """`(staff, None)` when the account may proceed, `(None, response)` when
    it may not -- 401 for no session, 403 for a session without this
    permission. Returned rather than raised so the call reads the same way as
    the `if staff_for(...) is None` line it replaces.
    """
    staff = staff_for(db, token)
    if staff is None:
        return None, unauthenticated()
    if not staff_admin.has_permission(staff, permission):
        return None, forbidden(permission)
    return staff, None
