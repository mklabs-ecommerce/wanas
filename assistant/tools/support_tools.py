"""Escalation and identity tools: request_human, get_my_profile, link_client."""

from __future__ import annotations

from assistant.messages import TOOL_RESULTS, USER
from assistant.tools.base import ToolContext, tool
from domain.models import HANDOFF_REASONS, Client, QueueKind
from domain.services import (
    identities,
    queues,
)

#: The reasons the **model** is allowed to hand a conversation over with.
#:
#: `HANDOFF_REASONS` is the wider set the *column* accepts, and the difference
#: between the two lists is the whole of this guard. `image_received` and
#: `voice_received` are raised by `assistant/runtime.py` before the model sees
#: anything, and `out_of_scope` by the channel adapters for a message type the
#: bot cannot open at all (a video, a sticker) -- none of the three is ever a
#: judgement the model makes, so offering all five in the tool schema was
#: offering three ways to leave a conversation that nothing behind the schema
#: would refuse.
MODEL_HANDOFF_REASONS = ("unclear", "complaint", "customer_asked")

#: Handoffs a customer's own next message may take back, if it arrives soon
#: enough -- see `assistant/recovery.py` for the window and the conditions.
#:
#: Both are handoffs raised over *a message*, not over a person's decision:
#: `unclear` means the bot could not read what was meant, and `out_of_scope`
#: (channel-side only, now that the model cannot raise it) means a sticker or
#: a video arrived that it cannot open. A customer who follows either of those
#: with an ordinary typed question has answered the thing that stopped the
#: conversation, and nothing about that needs a person.
#:
#: `complaint` and `customer_asked` are deliberately absent: both are a
#: person's judgement about a person, and the bot taking one of those back
#: would be the bot overruling a customer who asked for staff.
#: `image_received` and `voice_received` are absent too -- the media is still
#: unread, and it is what staff were queued to look at.
RESUMABLE_REASONS = ("unclear", "out_of_scope")

#: What a refused scope handoff hands back. The prompt has said since the
#: scope section was written that an off-topic question is answered with one
#: friendly line and **not** escalated ("ده مش سبب لـ request_human"), but the
#: tool schema listed `out_of_scope` as one of five options right next to it,
#: and a schema enum is a menu while a prompt line 200 lines away is a
#: preference. This is the tool that refuses, which is the standard every
#: other "never" in the prompt is already held to -- see the module docstring
#: on `assistant/prompt.py`, which named scope as the one rule with nothing
#: behind it.
_SCOPE_REFUSAL = {
    "error": "scope_is_not_a_handoff",
    "detail": (
        "A question outside the shop's business is not a reason to hand the "
        "conversation to a person -- escalating it puts trivia in the staff "
        "queue and leaves the customer waiting on someone who has nothing to "
        "say to them."
    ),
    "do_instead": (
        "Answer it yourself, in one friendly Arabic line that says you are "
        "only here for Wanas, and go straight back to what the customer was "
        "shopping for. Do not call this tool again for this."
    ),
}

#: What a first `unclear` handoff hands back. Step 2 of the prompt's handoff
#: section already says to ask one short clarifying question before escalating
#: an unclear message; this is the tool holding it to that, once. A second
#: `unclear` call in a *later* turn -- after the customer has answered the
#: clarifying question and it is still unclear -- goes through.
_CLARIFY_REFUSAL = {
    "error": "clarify_first",
    "detail": (
        "This conversation has not been asked a clarifying question yet. An "
        "unclear message is not by itself a reason to leave: a short reply "
        "like 'أيوه' or 'ده' is an answer to something you asked, and a "
        "handoff on it pauses the conversation until a person opens the "
        "dashboard."
    ),
    "do_instead": (
        "Re-read the recent messages for what the customer is pointing at, "
        "then ask ONE short clarifying question. If their next message is "
        "still unclear, call this tool again and it will go through."
    ),
}


def _clarifying_attempt_already_refused(history: list[dict] | None) -> bool:
    """Has this conversation already been sent back to ask a question?

    True only when the refusal is behind a message the *customer* has sent
    since -- otherwise the model could take the refusal and re-call the tool
    in the same breath, which is the guard reading its own output as the
    attempt it was asking for.
    """
    last_refusal = -1
    for index, message in enumerate(history or ()):
        if message.get("role") != TOOL_RESULTS:
            continue
        for result in message.get("results") or ():
            content = result.get("content")
            if (
                result.get("name") == "request_human"
                and isinstance(content, dict)
                and content.get("error") == "clarify_first"
            ):
                last_refusal = index
    if last_refusal < 0:
        return False
    return any(
        message.get("role") == USER for message in (history or ())[last_refusal + 1 :]
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

    The reason is validated here rather than only at the tool, so the runtime
    and the channel adapters cannot write a reason the queue's own vocabulary
    does not have.

    `RESUMABLE_REASONS` handoffs are stamped on the way out. That stamp is
    what `assistant/recovery.py` reads to decide whether a customer who writes
    back a few minutes later gets answered or sits behind a pause -- see that
    module.
    """
    if reason not in HANDOFF_REASONS:
        raise ValueError(f"unknown handoff reason {reason!r}")

    payload = dict(payload or {})
    if reason in RESUMABLE_REASONS:
        payload.setdefault("auto_resume_after_abandonment", True)

    item = queues.enqueue(
        session,
        kind=QueueKind.HANDOFF.value,
        reason=reason,
        summary=summary,
        channel=channel,
        external_id=external_id,
        payload=payload,
    )
    identities.pause(session, channel, external_id)
    return item


@tool(
    "request_human",
    "Hand this conversation to a person. This is the only way a conversation leaves you, and it is "
    "the last resort, not the answer to a message you did not follow. It pauses the conversation "
    "until a staff member picks it up -- you will not be asked to reply again, so tell the customer "
    "someone will get back to them. Use `complaint` for anything wrong with what arrived; never "
    "offer a new order to someone reporting a damaged or wrong item. A question that is not about "
    "the shop is NOT a reason to call this -- answer it yourself in one line and carry on.",
    properties={
        "reason": {
            "type": "string",
            "enum": list(MODEL_HANDOFF_REASONS),
            "description": (
                "unclear = you have already asked one clarifying question and still cannot tell "
                "what they mean. complaint = something is wrong with what arrived. "
                "customer_asked = they asked for a person. A question that is simply not about "
                "the shop is NOT one of these -- answer it in one line yourself."
            ),
        },
        "summary": {"type": "string", "description": "What staff need to know to pick this up cold."},
    },
    required=("reason", "summary"),
)
def request_human(ctx: ToolContext, reason: str, summary: str) -> dict:
    if reason == "out_of_scope":
        # Named separately from the `bad_arguments` branch below because the
        # answer is different: this is not a typo to correct, it is a
        # conversation the model was about to leave for a reason the prompt
        # has always said is not one.
        return dict(_SCOPE_REFUSAL)

    if reason not in MODEL_HANDOFF_REASONS:
        return {
            "error": "bad_arguments",
            "detail": f"reason must be one of {', '.join(MODEL_HANDOFF_REASONS)}",
        }

    if reason == "unclear" and not _clarifying_attempt_already_refused(ctx.history):
        return dict(_CLARIFY_REFUSAL)

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
