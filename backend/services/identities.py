"""Channel identities and the confirmed-link rule.

`client_id` stays null until the customer confirms the link. A conversation, a
session and a cart can all belong to nobody right up until the first order.

An automatic link is the same operation as showing one customer another
customer's saved address and order history: phone numbers get reused by
carriers, shared within a household, and mistyped, so an exact match is not
proof of identity. Match, then ask -- never link silently.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.models import ChannelIdentity, Client, utcnow


def get_or_create(session: Session, channel: str, external_id: str) -> ChannelIdentity:
    identity = session.get(ChannelIdentity, (channel, external_id))
    if identity is None:
        identity = ChannelIdentity(channel=channel, external_id=external_id)
        session.add(identity)
        session.flush()
    identity.last_seen_at = utcnow()
    return identity


def get(session: Session, channel: str, external_id: str) -> ChannelIdentity | None:
    return session.get(ChannelIdentity, (channel, external_id))


def client_for(session: Session, channel: str, external_id: str) -> Client | None:
    identity = get(session, channel, external_id)
    if identity is None or identity.client_id is None:
        return None
    return session.get(Client, identity.client_id)


def find_matching_client(
    session: Session, *, phone: str | None = None, email: str | None = None
) -> Client | None:
    clauses = []
    if phone:
        for candidate in phone_variants(phone):
            clauses.append(Client.phone == candidate)
    if email:
        clauses.append(Client.email == email.strip().lower())
    if not clauses:
        return None
    return session.scalar(select(Client).where(or_(*clauses)).order_by(Client.client_id))


def phone_variants(phone: str) -> list[str]:
    """The same Egyptian number written the handful of ways people write it.

    WhatsApp hands over `201001234567`; a customer types `01001234567`. Without
    this they are two different people and a returning customer is asked to
    retype an address we already have.
    """
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return []
    variants = {digits}
    if digits.startswith("20"):
        variants.add("0" + digits[2:])
        variants.add(digits[2:])
    elif digits.startswith("0"):
        variants.add("20" + digits[1:])
        variants.add(digits[1:])
    else:
        variants.add("0" + digits)
        variants.add("20" + digits)
    return sorted(variants)


def note_pending_link(session: Session, identity: ChannelIdentity, client: Client, matched_on: str) -> None:
    """Record an unconfirmed match for `get_my_profile` to surface.

    The name is masked because an unconfirmed match is, by definition,
    possibly a stranger -- showing a full name before confirmation leaks it to
    whoever typed the number.
    """
    identity.pending_link = {
        "client_id": client.public_id,
        "matched_on": matched_on,
        "masked_name": mask_name(client.full_name),
        "_client_pk": client.client_id,
    }
    session.flush()


def mask_name(full_name: str) -> str:
    parts = [p for p in (full_name or "").split() if p]
    return " ".join(f"{p[0]}…" for p in parts) or "…"


def link(session: Session, identity: ChannelIdentity, client_id: int) -> None:
    identity.client_id = client_id
    identity.pending_link = None
    session.flush()


def decline_link(session: Session, identity: ChannelIdentity) -> None:
    """Declined leaves the two separate, and a fresh record is created at
    checkout."""
    identity.pending_link = None
    session.flush()


def detect_pending_link_from_external_id(session: Session, identity: ChannelIdentity) -> None:
    """WhatsApp's external_id *is* a phone number, so a returning customer can
    be spotted on first contact rather than only at checkout."""
    if identity.client_id is not None or identity.pending_link:
        return
    if identity.channel != "whatsapp":
        return
    match = find_matching_client(session, phone=identity.external_id)
    if match is not None:
        note_pending_link(session, identity, match, "phone")


def pause(session: Session, channel: str, external_id: str) -> ChannelIdentity:
    identity = get_or_create(session, channel, external_id)
    identity.paused_until_staff_reply = True
    session.flush()
    return identity


def unpause(session: Session, channel: str, external_id: str) -> None:
    """Only ever called from a staff resolving the handoff. Not a timer, and
    not the model deciding the conversation looks normal again."""
    identity = get(session, channel, external_id)
    if identity is not None:
        identity.paused_until_staff_reply = False
        session.flush()


def is_paused(session: Session, channel: str, external_id: str) -> bool:
    identity = get(session, channel, external_id)
    return bool(identity and identity.paused_until_staff_reply)
