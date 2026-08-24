"""Instagram's 60-day token, kept alive automatically.

An Instagram long-lived token expires 60 days after issuance and there is no
refresh-token flow: the token refreshes *itself*, via `refresh_access_token`,
but only while it is still valid and at least 24 hours old. Nothing else in
the system notices the difference between a working token and one that died
this morning -- both are just Bearer strings until graph.instagram.com starts
answering 190. So this runs on the scheduler (`domain/services/scheduler.py`)
and on startup: refresh when the stored expiry is inside ten days, record the
new token here, and alert a person when the refresh itself fails.

The database row is authoritative once it exists. A token refreshed and then
never read is the same as no refresh at all, so `InstagramClient.__init__`
reads this row first and falls back to `INSTAGRAM_ACCESS_TOKEN` only when no
row exists yet.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from common.timeutil import as_aware
from config.settings import settings
from domain.db import session_scope
from domain.models import IntegrationToken, QueueKind, utcnow

log = logging.getLogger("wanas.instagram_token")

PROVIDER = "instagram"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"

#: Refresh when the stored expiry is closer than this. Ten days of margin
#: means several daily chances before anything can expire.
REFRESH_AHEAD = timedelta(days=10)

#: Meta documents ~60 days; used only when a response somehow omits
#: `expires_in`, so the next check still sees an expiry at all.
DEFAULT_EXPIRES_IN = 60 * 24 * 3600

#: The scheduler loop runs every ~30 minutes; the refresh attempt is meant to
#: happen once a day at most. Module state, deliberately: losing it to a
#: redeploy just means one extra cheap check.
_MIN_ATTEMPT_INTERVAL = timedelta(hours=24)
_last_attempt_at = None


class TokenRefreshFailed(Exception):
    pass


def stored_token() -> str | None:
    """The credential outbound sends should use, or None.

    The DB row wins over the env var once one exists -- that is the entire
    point of writing refreshed tokens down. Never raises: a client built
    before the schema exists (or with no database reachable) falls back to
    configuration, which is the inert-but-working contract every other piece
    of optional infrastructure here follows.
    """
    try:
        with session_scope() as session:
            row = session.get(IntegrationToken, PROVIDER)
            if row is not None and row.access_token:
                return row.access_token
    except Exception:
        log.exception("could not read the stored instagram token")
    return None


def expires_at():
    """The stored expiry for `/health`, or None."""
    try:
        with session_scope() as session:
            row = session.get(IntegrationToken, PROVIDER)
            return as_aware(row.expires_at) if row else None
    except Exception:
        return None


def _due(row: IntegrationToken | None) -> bool:
    if row is None:
        # No row yet: refresh the configured env token once, so the row comes
        # into existence long before the original could age out.
        return bool(settings.instagram_configured)
    if row.expires_at is None:
        # Unknown expiry is treated as urgent -- there is nothing to wait for.
        return True
    return (as_aware(row.expires_at) - utcnow()) < REFRESH_AHEAD


def maybe_refresh(*, force: bool = False) -> bool:
    """Refresh the Instagram token when it is due.

    Returns True when a refresh succeeded this call. Rate-limited to one
    attempt per day from the scheduler's faster loop; `force` bypasses that
    rate limit for the startup call and tests.
    """
    global _last_attempt_at

    now = utcnow()
    if (
        not force
        and _last_attempt_at is not None
        and now - _last_attempt_at < _MIN_ATTEMPT_INTERVAL
    ):
        return False
    _last_attempt_at = now

    with session_scope() as session:
        row = session.get(IntegrationToken, PROVIDER)
        due = _due(row)
        current = row.access_token if row else settings.instagram_access_token

    if not due or not current:
        return False

    try:
        data = _call_refresh(current)
    except Exception as exc:
        log.error("instagram token refresh failed: %s", exc)
        _alert(str(exc)[:300])
        return False

    new_token = data.get("access_token")
    if not new_token:
        log.error("instagram token refresh returned no access_token")
        _alert("response carried no access_token")
        return False

    expires_in = int(data.get("expires_in") or DEFAULT_EXPIRES_IN)
    with session_scope() as session:
        row = session.get(IntegrationToken, PROVIDER)
        if row is None:
            row = IntegrationToken(provider=PROVIDER, access_token=new_token)
            session.add(row)
        else:
            row.access_token = new_token
        row.expires_at = utcnow() + timedelta(seconds=expires_in)
        row.refreshed_at = utcnow()

    log.info(
        "instagram access token refreshed; new expiry in ~%.0f days",
        expires_in / 86400,
    )
    return True


def _call_refresh(current_token: str) -> dict:
    response = httpx.get(
        REFRESH_URL,
        params={"grant_type": "ig_refresh_token", "access_token": current_token},
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise TokenRefreshFailed(f"{response.status_code}: {response.text[:200]}")
    return response.json()


def _alert(detail: str) -> None:
    """A token nine days from death is a staff problem, not a silent one."""
    try:
        with session_scope() as session:
            from domain.services import queues

            queues.enqueue(
                session,
                kind=QueueKind.ALERT.value,
                reason="instagram_token_refresh_failed",
                summary="Instagram access token could not be refreshed; "
                "the channel dies when the current one expires",
                payload={"detail": detail},
            )
    except Exception:
        log.exception("could not enqueue the instagram token alert")


# --------------------------------------------------------------------------
# scheduler wiring
# --------------------------------------------------------------------------


def scheduled_refresh() -> None:
    """The `_tick` job: rate-limited, exception-proofed like its two siblings."""
    try:
        if maybe_refresh():
            return
    except Exception:
        log.exception("instagram token refresh check failed")
