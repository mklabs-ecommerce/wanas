"""Staff authentication, and the dashboard session built on top of it.

One role: everyone who can log in can do everything. What is not optional at
one role is that passwords are hashed, there is no shared login, and every
action is attributed -- attribution is the only control this model has.

PBKDF2-HMAC-SHA256 from the standard library, so there is no native build step
on any platform the shop might deploy from. The session token below is HMAC
over the same standard library, for the same reason.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import settings
from domain.models import Staff

_ALGO = "sha256"
_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo_part, iterations, salt_hex, digest_hex = encoded.split("$")
        algo = algo_part.split("_", 1)[1]
        computed = hashlib.pbkdf2_hmac(
            algo, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, IndexError):
        return False
    # Constant-time: a timing difference on password comparison is a real leak.
    return hmac.compare_digest(computed.hex(), digest_hex)


def create_staff(session: Session, username: str, password: str) -> Staff:
    username = username.strip()
    if not username:
        raise ValueError("username is required")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    if session.scalar(select(Staff).where(Staff.username == username)):
        raise ValueError(f"staff user {username!r} already exists")
    staff = Staff(username=username, password_hash=hash_password(password), is_active=True)
    session.add(staff)
    session.flush()
    return staff


#: A real hash of a value nobody can log in with, verified against whenever
#: there is no account to verify against. See `authenticate`.
_DUMMY_HASH = hash_password("no-such-account-" + os.urandom(16).hex())


def authenticate(session: Session, username: str, password: str) -> Staff | None:
    """None for a wrong password, an unknown username, and a deactivated
    account alike -- and in the same amount of time.

    The reply body has always been the same for all three
    (`invalid_credentials`, never "no such user"), but the *timing* was not:
    an unknown username returned before any hashing happened, while a real one
    cost 240,000 rounds of PBKDF2 first. That difference is tens of
    milliseconds, which is comfortably measurable over the internet, so the
    login page answered "does this username exist?" to anyone willing to time
    it -- which is the first half of a targeted password attack against a
    dashboard that can read every customer's address and order history.

    So the hash is computed either way. `_DUMMY_HASH` is a genuine PBKDF2
    digest at the same cost, not a sleep: it stays correct if the iteration
    count changes, and there is no timing to tune.
    """
    staff = session.scalar(select(Staff).where(Staff.username == username.strip()))
    if staff is None or not staff.is_active:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, staff.password_hash):
        return None
    return staff


# --------------------------------------------------------------------------
# Dashboard sessions
#
# A signed, stateless cookie rather than a sessions table: one more thing
# that must survive a restart for a shop this size is not worth the table.
# `staff_id.expires_at` signed with HMAC -- forging one means guessing
# `DASHBOARD_SESSION_SECRET`, and there is nothing to look up, so a login
# check costs one query instead of a join on every request.
# --------------------------------------------------------------------------


def issue_session_token(staff: Staff) -> str:
    expires_at = int(time.time()) + settings.dashboard_session_hours * 3600
    payload = f"{staff.staff_id}.{expires_at}"
    signature = hmac.new(
        settings.dashboard_session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def staff_from_session_token(session: Session, token: str | None) -> Staff | None:
    """None on anything wrong with the token -- expired, tampered with, or the
    account deactivated since it was issued. Never raises: a malformed cookie
    is exactly as unauthenticated as no cookie at all."""
    if not settings.dashboard_session_secret or not token:
        return None
    try:
        staff_id_part, expires_part, signature = token.split(".")
        staff_id = int(staff_id_part)
        expires_at = int(expires_part)
    except (ValueError, AttributeError):
        return None

    payload = f"{staff_id_part}.{expires_part}"
    expected = hmac.new(
        settings.dashboard_session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    if expires_at < int(time.time()):
        return None

    staff = session.get(Staff, staff_id)
    if staff is None or not staff.is_active:
        return None
    return staff

