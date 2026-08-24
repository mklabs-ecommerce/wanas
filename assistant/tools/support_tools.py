"""Escalation and identity tools: request_human, get_my_profile, link_client."""

from __future__ import annotations

from assistant.tools.base import ToolContext, tool
from domain.models import HANDOFF_REASONS, Client, QueueKind
from domain.services import (
    identities,
    queues,
)


def raise_handoff(
    session,
    channel: str,
    external_id: str,
    reason: str,
    summary: str,
    payload: dict | None = None,
):
    """Queue a handoff and pause the conversation.

    Shared by the `request_human` tool and by the runtime, which raises
    `image_received` itself before the model sees anything. One implementation,
    so a conversation can never end up queued but still answered.
    """
    item = queues.enqueue(
        session,
        kind=QueueKind.HANDOFF.value,
        reason=reason,
        summary=summary,
        channel=channel,
        external_id=external_id,
        payload=payload or {},
    )
    identities.pause(session, channel, external_id)
    return item


@tool(
    "request_human",
    "Hand this conversation to a person. This is the only way a conversation leaves you. It pauses "
    "the conversation until a staff member picks it up -- you will not be asked to reply again, so "
    "tell the customer someone will get back to them. Use `complaint` for anything wrong with what "
    "arrived; never offer a new order to someone reporting a damaged or wrong item.",
    properties={
        "reason": {
            "type": "string",
            "description": "unclear | complaint | customer_asked | image_received | out_of_scope",
        },
        "summary": {"type": "string", "description": "What staff need to know to pick this up cold."},
    },
    required=("reason", "summary"),
)
def request_human(ctx: ToolContext, reason: str, summary: str) -> dict:
    if reason not in HANDOFF_REASONS:
        return {"error": "bad_arguments", "detail": f"reason must be one of {', '.join(HANDOFF_REASONS)}"}

    # The pause flag lives on the channel identity. While it is set the runtime
    # stops calling the model for this conversation entirely; incoming messages
    # are stored and shown to staff. Only a staff action in the dashboard
    # clears it -- not a timer, and not the model deciding things look normal
    # again, because a human is handling it now.
    raise_handoff(ctx.session, ctx.channel, ctx.external_id, reason, summary)
    return {"queued": True, "conversation_paused": True}


@tool(
    "get_my_profile",
    "What we already know about whoever is messaging. Call this before asking for shipping details "
    "so a returning customer confirms a saved address instead of retyping it. known:false is the "
    "normal state for a first-time customer, not an error. If pending_link is set, ask 'is this "
    "you?' and then call link_client with their answer.",
)
def get_my_profile(ctx: ToolContext) -> dict:
    identity = identities.get(ctx.session, ctx.channel, ctx.external_id)
    pending = dict(identity.pending_link) if (identity and identity.pending_link) else None
    if pending:
        pending.pop("_client_pk", None)  # internal key, never shown to the model

    client = identities.client_for(ctx.session, ctx.channel, ctx.external_id)
    if client is None:
        return {"known": False, "pending_link": pending}

    return {
        "known": True,
        "client_id": client.public_id,
        "full_name": client.full_name,
        "phone": client.phone,
        "email": client.email,
        "governorate": client.governorate,
        "address": client.address,
        "pending_link": pending,
    }


@tool(
    "link_client",
    "Answer the 'is this you?' question raised by pending_link. true attaches this conversation to "
    "the existing customer record; false leaves them separate. Nothing is ever linked without this "
    "call.",
    properties={"confirmed": {"type": "boolean"}},
    required=("confirmed",),
)
def link_client(ctx: ToolContext, confirmed: bool) -> dict:
    identity = identities.get(ctx.session, ctx.channel, ctx.external_id)
    if identity is None or not identity.pending_link:
        return {"error": "no_pending_link"}

    if not confirmed:
        # Declined leaves the two records separate, which is a supported
        # outcome, not a failure.
        identities.decline_link(ctx.session, identity)
        return {"linked": False}

    client_pk = identity.pending_link.get("_client_pk")
    client = ctx.session.get(Client, client_pk) if client_pk else None
    if client is None:
        identities.decline_link(ctx.session, identity)
        return {"error": "client_not_found"}

    identities.link(ctx.session, identity, client.client_id)
    return {
        "linked": True,
        "client_id": client.public_id,
        "full_name": client.full_name,
        "governorate": client.governorate,
        "address": client.address,
    }
