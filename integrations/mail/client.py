"""SMTP, over the standard library, for the owner's alert emails.

One vendor package like every other under `integrations/`, and the only place
in the codebase that opens a socket to a mail server. The policy of *what* is
worth an email lives in `domain/services/alert_email.py`; this file knows how
to send one and nothing else.

Gmail is the intended host (`smtp.gmail.com:587`, STARTTLS, a 16-character App
Password), but nothing here is Gmail-specific -- any SMTP server with the same
four variables works.

There is no new dependency: `smtplib` and `email.message` ship with Python.
An email provider's SDK would be a fifth vendor to keep a key for, and this
sends a handful of plain-text messages a week.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from config.settings import settings

log = logging.getLogger("wanas.mail")

#: Never leave a worker thread hanging on an unreachable mail host. An alert
#: that arrives two minutes late is fine; a thread that never returns is not.
TIMEOUT_SECONDS = 20


def send_email(subject: str, body: str) -> bool:
    """Send one plain-text message to the configured owner address.

    Returns whether it left the building. Raises nothing: an alert email is
    the *second* record of something that is already in the staff queue, so a
    mail outage must never be able to break the path that raised it.
    """
    if not settings.alert_email_configured:
        log.debug("alert email not configured; not sending %r", subject)
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
