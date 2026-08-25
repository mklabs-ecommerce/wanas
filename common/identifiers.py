"""Telling a WhatsApp phone number from a business-scoped user id.

Meta used to identify every customer by their phone number: `messages[].from`
inbound, `to` outbound. From April 2026 it also sends a **business-scoped user
id** (BSUID) -- `messages[].from_user_id` and `contacts[].user_id`, always
present -- and for a customer who uses a WhatsApp username it sends *only*
that: `from` and `wa_id` are omitted entirely.

The identifier looks like `EG.1754797805572316`: an ISO 3166 alpha-2 country
code, a period, and up to 128 alphanumerics. It is scoped to one business
portfolio, so it is stable for this shop and meaningless anywhere else.

This lives in `common/` because three layers need the same answer and none of
them may import each other: the channel adapter reads the identity, the
outbound client has to address it (`recipient`, not `to`), and
`domain/services/identities.py` must not try to match it against a customer's
phone number -- stripping `EG.` leaves digits that would link a stranger.
"""

from __future__ import annotations

import re

#: Deliberately anchored and narrow. Anything that is not unmistakably a BSUID
#: is treated as a phone number, which is what every identifier in this system
#: was before April 2026 and what the overwhelming majority still are.
_BSUID = re.compile(r"^[A-Za-z]{2}\.[A-Za-z0-9]{1,128}$")


def is_bsuid(value: str | None) -> bool:
    """Whether this identifier is a business-scoped user id rather than a phone
    number. False for anything empty, malformed, or made of digits."""
    return bool(value) and _BSUID.match(value or "") is not None


def is_phone_number(value: str | None) -> bool:
    """Whether this identifier can be treated as a phone number at all.

    Not a validity check -- `phone_variants` and `normalise_recipient` already
    own that. This only answers "is it safe to run phone logic over this", so
    a BSUID never reaches code that assumes digits.
    """
    return bool(value) and not is_bsuid(value) and any(ch.isdigit() for ch in value)
