"""Dashboard authentication.

The dashboard is never reachable without logging in. It can edit stock and
orders; an accidentally-public deployment is a live business risk, not a test
inconvenience -- so the check is a router-wide dependency rather than
something each route remembers to add.

One role, so attribution is the only control there is: every staff-initiated
change records who did it.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from backend.config import settings
from backend.db import get_session
from backend.models import Staff

COOKIE_NAME = "wanas_staff"
CSRF_FIELD = "csrf_token"

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="wanas-dashboard")


def _redirect_to_login() -> HTTPException:
    """A browser hitting a protected page should land on the login form, not
    on a JSON 401. A 303 with a Location header does that without needing an
    app-level exception handler, so mounting this router is all it takes to be
    protected."""
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="login required",
        headers={"Location": "/dashboard/login"},
    )


def issue_session(response, staff: Staff) -> str:
    csrf = secrets.token_urlsafe(24)
    token = _serializer.dumps({"staff_id": staff.staff_id, "username": staff.username, "csrf": csrf})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        # Set Secure when the dashboard is served over TLS; left off so a
        # local http:// deployment can still log in.
        secure=False,
    )
    return csrf


def clear_session(response) -> None:
    response.delete_cookie(COOKIE_NAME)


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        # Sessions expire: a staff login left open on a shared machine should
        # not stay valid indefinitely.
        return _serializer.loads(raw, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None


def current_staff(request: Request, db: Session = Depends(get_session)) -> Staff:
    data = read_session(request)
    if not data:
        raise _redirect_to_login()
    staff = db.get(Staff, data.get("staff_id"))
    if staff is None or not staff.is_active:
        # Deactivating a login takes effect immediately, even mid-session.
        raise _redirect_to_login()
    request.state.csrf = data.get("csrf")
    return staff


def verify_csrf(request: Request, submitted: str | None) -> None:
    """State-changing requests carry a token from the session cookie.

    Without it, any page a logged-in staff member visits could silently POST a
    price change to this dashboard.
    """
    data = read_session(request)
    expected = (data or {}).get("csrf")
    if not expected or not submitted or not secrets.compare_digest(str(expected), str(submitted)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)
