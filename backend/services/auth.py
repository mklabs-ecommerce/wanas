"""Staff authentication.

One role: everyone who can log in can do everything. What is not optional at
one role is that passwords are hashed, there is no shared login, and every
action is attributed -- attribution is the only control this model has.

PBKDF2-HMAC-SHA256 from the standard library, so there is no native build step
on any platform the shop might deploy from.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Staff

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
        computed = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
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


def authenticate(session: Session, username: str, password: str) -> Staff | None:
    staff = session.scalar(select(Staff).where(Staff.username == username.strip()))
    if staff is None or not staff.is_active:
        return None
    if not verify_password(password, staff.password_hash):
        return None
    return staff

