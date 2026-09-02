"""Getting the owner's alert emails out, by whichever route the host allows.

One vendor package like every other under `integrations/`, and the only place
in the codebase that talks to a mail service. The policy of *what* is worth an
email lives in `domain/services/alert_email.py`; this file knows how to send
one and nothing else.

**Two transports, because one of them cannot work in production.** Railway
blocks every outbound SMTP port -- 25, 465, 587 and 2525 all answer `Network
is unreachable` after the full connect timeout from inside the container,
while plain HTTP connects in milliseconds. That is a platform policy against
spam, so an app password is worthless there however correct it is. The Gmail
API (`gmail_api.py`) sends as the same mailbox over 443 and is what actually
delivers from the deploy.

SMTP stays, and is not dead code: it is what a developer's machine has, what
most other hosts have, and what makes this file portable off Railway. It is
also the fallback that keeps alerts flowing if the Gmail OAuth grant is ever
revoked while running somewhere SMTP is permitted.

`send_email` picks: the Gmail API when its three OAuth values are set, else
SMTP. Neither configured is a documented off state, not an error -- every one
of these alerts still reaches the dashboard queue.

There is no new dependency either way: `smtplib`, `email.message` and
`base64` ship with Python, and `httpx` is already here for every other
vendor. An email provider's SDK would be a fifth vendor to keep a key for,
and this sends a handful of plain-text messages a week.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from config.settings import settings
from integrations.mail import gmail_api

log = logging.getLogger("wanas.mail")

#: Never leave a worker thread hanging on an unreachable mail host. An alert
#: that arrives two minutes late is fine; a thread that never returns is not.
TIMEOUT_SECONDS = 20


def send_email(subject: str, body: str) -> bool:
    """Send one alert to the owner, over whichever transport is configured.

    The Gmail API wins when it is set up, because it is the only one that can
    leave a Railway container. Returns whether the message was accepted, and
    raises nothing -- see `send_over_smtp`.
    """
    if settings.gmail_api_configured:
        return gmail_api.send_email(subject, body)
    return send_over_smtp(subject, body)


def send_over_smtp(subject: str, body: str) -> bool:
    """Send one plain-text message to the configured owner address.

    Returns whether it left the building. Raises nothing: an alert email is
    the *second* record of something that is already in the staff queue, so a
    mail outage must never be able to break the path that raised it.
    """
    if not (settings.alert_smtp_configured and settings.alert_email_to):
        log.debug("smtp alert email not configured; not sending %r", subject)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.alert_email_from or settings.alert_smtp_username
    message["To"] = settings.alert_email_to
    message.set_content(body)

    try:
        if settings.alert_smtp_port == 465:
            with smtplib.SMTP_SSL(
                settings.alert_smtp_host,
                settings.alert_smtp_port,
                timeout=TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            ) as smtp:
                smtp.login(settings.alert_smtp_username, settings.alert_smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(
                settings.alert_smtp_host, settings.alert_smtp_port, timeout=TIMEOUT_SECONDS
            ) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(settings.alert_smtp_username, settings.alert_smtp_password)
                smtp.send_message(message)
    except Exception:
        # Deliberately without the exception's own arguments in the message:
        # an SMTP auth failure echoes the username, and some servers echo more.
        log.exception("could not send the alert email %r", subject)
        return False
    log.info("alert email sent: %s", subject)
    return True
