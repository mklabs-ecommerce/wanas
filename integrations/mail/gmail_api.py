"""Gmail over HTTPS, because Railway will not let SMTP out.

Every outbound SMTP port is blocked from inside the container -- 25, 465, 587
and 2525 all answer `Network is unreachable` after the full connect timeout,
while plain HTTP connects in milliseconds. That is a platform policy against
spam, not a setting, so an app password is worthless there however correct
the credentials are.

The Gmail API sends as the *same mailbox* over 443, costs nothing, and adds
no vendor: quota is a billion units a day against 100 per send, and the real
ceiling is Gmail's own ~500 recipients a day, against the handful of alerts
this shop raises.

What it costs instead is OAuth. Three values, all from `.env` / Railway and
never logged:

    GMAIL_CLIENT_ID      from a Google Cloud project's OAuth client
    GMAIL_CLIENT_SECRET  the same
    GMAIL_REFRESH_TOKEN  minted once by scripts/gmail_authorise.py

**The refresh token is the thing that can quietly die**, which is the known
cost of this route. Google expires it after seven days while the OAuth
consent screen is still in *Testing*; publishing the app (even unverified)
is what makes it durable. It also dies on a password change or a revoked
grant. Failures here are loud in the log for exactly that reason -- there is
no second channel to warn through, since the thing that broke *is* the
warning channel.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from email.message import EmailMessage

import httpx

from config.settings import settings

log = logging.getLogger("wanas.mail.gmail")

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

#: Never leave a worker thread hanging on a slow Google. An alert that
#: arrives a minute late is fine; a thread that never returns is not.
TIMEOUT_SECONDS = 20

#: Access tokens last an hour. Refreshed a minute early so a token cannot
#: expire between the check and the send.
_EXPIRY_MARGIN_SECONDS = 60

_lock = threading.Lock()
_access_token: str = ""
_expires_at: float = 0.0


def reset_token_cache() -> None:
    """For tests, and for anything that changes the credentials at runtime."""
    global _access_token, _expires_at
    with _lock:
        _access_token = ""
        _expires_at = 0.0


def _fetch_access_token() -> str:
    """Trade the refresh token for a short-lived access token.

    Cached under a lock: the alert path runs on its own daemon thread and two
    alerts arriving together must not both spend a round trip -- nor race to
    write the cache.
    """
    global _access_token, _expires_at
    with _lock:
        if _access_token and time.monotonic() < _expires_at:
            return _access_token
        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret,
                "refresh_token": settings.gmail_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            # Google echoes nothing secret in this body, but it is a token
            # endpoint -- only the error code is logged, never the payload.
            try:
                kind = (response.json() or {}).get("error") or "unknown_error"
            except ValueError:
                kind = "unparseable_response"
            raise RuntimeError(
                f"Gmail refused the refresh token ({response.status_code}: {kind}). "
                "If this is invalid_grant the token has expired or been revoked -- "
                "mint a new one with scripts/gmail_authorise.py, and check the OAuth "
                "consent screen is published rather than left in Testing, which "
                "expires refresh tokens after seven days."
            )
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError("Gmail returned no access token")
        _access_token = token
        _expires_at = time.monotonic() + max(
            int(payload.get("expires_in") or 3600) - _EXPIRY_MARGIN_SECONDS, 0
        )
        return token


def send_email(subject: str, body: str) -> bool:
    """Send one plain-text message to the configured owner address.

    Returns whether Gmail accepted it. Raises nothing: an alert email is the
    *second* record of something already written to the staff queue, so a mail
    outage must never break the path that raised it.
    """
    if not (settings.gmail_api_configured and settings.alert_email_to):
        log.debug("gmail api not configured; not sending %r", subject)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    # Gmail will not let you forge this -- it rewrites From to the
    # authenticated mailbox -- but sending it keeps the header honest when the
    # two do agree.
    message["From"] = settings.alert_email_from or settings.alert_smtp_username
    message["To"] = settings.alert_email_to
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    try:
        token = _fetch_access_token()
        response = httpx.post(
            SEND_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"raw": raw},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code == 401:
            # The cached access token was rejected -- it may have been
            # revoked mid-life. One retry with a fresh one, then give up:
            # anything more is a loop against an endpoint that is refusing us.
            reset_token_cache()
            response = httpx.post(
                SEND_URL,
                headers={"Authorization": f"Bearer {_fetch_access_token()}"},
                json={"raw": raw},
                timeout=TIMEOUT_SECONDS,
            )
        if response.status_code >= 400:
            log.error(
                "gmail refused the alert email %r (%s): %s",
                subject,
                response.status_code,
                response.text[:300],
            )
            return False
    except Exception:
        # Deliberately without the exception's own arguments in the message:
        # this path handles tokens, and a stray echo of one in a log is the
        # thing the whole credential rule exists to prevent.
        log.exception("could not send the alert email %r over the Gmail API", subject)
        return False

    log.info("alert email sent over the Gmail API: %s", subject)
    return True
