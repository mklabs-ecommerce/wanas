"""Resend over HTTPS -- the transport with nothing in it that expires.

Same problem as `gmail_api.py` solves and the same reason SMTP cannot: Railway
blocks every outbound SMTP port from inside the container, so an app password
is worthless there however correct it is. This route sends over 443 like the
Gmail one.

What it buys over Gmail is that **there is no OAuth grant to die.** The Gmail
route's known cost is its refresh token -- Google expires it after seven days
while the consent screen is in Testing, and it also dies on a password change
or a revoked grant. That failure is silent in the worst possible way, because
the thing that breaks *is* the warning channel. Resend is one long-lived API
key and one POST.

    RESEND_API_KEY  from the Resend dashboard
    RESEND_FROM     the sender address; must be on a domain verified with
                    Resend, or left at the shared `onboarding@resend.dev`,
                    which is allowed without verification but will only
                    deliver to the Resend account owner's own address. That
                    is exactly who these alerts go to, so the default works
                    unconfigured -- but a verified domain is what stops the
                    owner's inbox filing the shop's alerts as somebody
                    else's mail.

`client.py` prefers this over Gmail over SMTP. The policy of *what* is worth
an email is still `domain/services/alert_email.py`; this file knows how to
send one and nothing else.
"""

from __future__ import annotations

import logging

import httpx

from config.settings import settings

log = logging.getLogger("wanas.mail.resend")

SEND_URL = "https://api.resend.com/emails"

#: Never leave a worker thread hanging on a slow vendor. An alert that
#: arrives a minute late is fine; a thread that never returns is not.
TIMEOUT_SECONDS = 20


def send_email(subject: str, body: str) -> bool:
    """Send one plain-text message to the configured owner address.

    Returns whether Resend accepted it. Raises nothing: an alert email is the
    *second* record of something already written to the staff queue, so a mail
    outage must never break the path that raised it.
    """
    if not (settings.resend_configured and settings.alert_email_to):
        log.debug("resend not configured; not sending %r", subject)
        return False

    try:
        response = httpx.post(
            SEND_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.resend_from,
                "to": [settings.alert_email_to],
                "subject": subject,
                "text": body,
            },
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            # The body carries Resend's own reason -- an unverified sender
            # domain being the one that actually happens -- and no credential.
            log.error(
                "resend refused the alert email %r (%s): %s",
                subject,
                response.status_code,
                response.text[:300],
            )
            return False
    except Exception:
        # Deliberately without the exception's own arguments: this path
        # carries an API key in a header, and a stray echo of one in a log is
        # the thing the whole credential rule exists to prevent.
        log.exception("could not send the alert email %r over Resend", subject)
        return False

    log.info("alert email sent over Resend: %s", subject)
    return True
