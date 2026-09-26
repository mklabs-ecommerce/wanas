"""Ordering and after-the-order tools.

confirm_order, get_my_orders, modify_order_quantity, add_item_to_order,
cancel_order, get_return_terms, request_item_swap, request_item_add,
submit_feedback.

**Adding to an unshipped order applies immediately; adding to a shipped one
is a request.** `add_item_to_order` is `modify_order_quantity`'s sibling --
same immediate-Shopify-edit shape, gated the same way by `order.modifiable`
-- now that the app carries `write_order_edits`. `request_item_add` stays for
the cases that still need a person: a shipped order, or a customer who has
described what they want without naming a variant.

**Adding and swapping are two tools because they are two things.** For a
while `request_item_swap` was the only post-order request type there was, so
"ينفع اضيفه على نفس الاوردر اللي فات" -- add this piece to the order I already
placed -- had no shape of its own to go into, and went into the swap: filed as
"take the Knitted Polo off, put the Heart Top on", against a line the customer
had never mentioned and still wanted. One staff click from a garment leaving
an order. The two tools now differ by the thing that matters: `from_variant_id`
is required on a swap and does not exist on an add, so an add *cannot* name
something to remove. Where the customer's wording could be either, neither
tool is the safe guess -- ask which, in one short question.

The status rule lives here, not in the prompt: modify and cancel check the
order's status themselves and refuse a shipped order regardless of how the
request was phrased. A prompt rule would be a suggestion the model could talk
itself out of.

The *terms* follow the same rule. A refusal that only says "not modifiable"
leaves the model to invent what happens next, and what happens next costs
money: a shipped order can only come back by being refused at the door, and
that is a return the customer pays the round trip on -- both the delivery
attempt and the trip back. So `cancel_order` refuses a shipped order with the
route and the arithmetic attached (`orders.return_terms`), and
`get_return_terms` answers the question on its own for a customer who asks
before naming an order.
"""

from __future__ import annotations

import re

from assistant.messages import USER
from assistant.tools.base import ToolContext, tool
from common.identifiers import is_phone_number
from domain.models import OrderStatus, Variant
from domain.services import identities, orders
from domain.services.notifications import item_add_requested, item_swap_requested


@tool(
    "confirm_order",
    "Place the order. This is the only tool that writes one, and it re-checks live stock itself. "
    "Do not tell the customer an order was placed until this returns an order_id. Requires the "
    "customer's name, the governorate (a picked value, not inferred from the address text), the "
    "full address and a contact phone. Cash on delivery only. contact_phone must be an Egyptian "
    "mobile the customer actually gave -- typed in this conversation, saved on their profile, or "
    "the WhatsApp number they are writing from; never one you reconstructed. invalid_phone means "
    "it is not a mobile a courier can call; phone_not_given means they never sent it -- ask them "
    "to type it. not_confirmed_by_customer means your last message was not a summary with the "
    "checkout total, or the customer did not say yes to it: send the summary, ask «أأكد "
    "الأوردر؟», and call this again after they agree.",
    properties={
        "customer_name": {"type": "string"},
        "governorate": {"type": "string", "description": "From the fixed list; ask, do not infer."},
        "address": {"type": "string", "description": "Street, building, apartment, landmark."},
        "contact_phone": {"type": "string"},
        "email": {"type": "string", "description": "Optional; the WhatsApp flow does not ask for one."},
    },
    required=("customer_name", "governorate", "address", "contact_phone"),
    missing_error="missing_fields",
)
def confirm_order(
    ctx: ToolContext,
    customer_name: str,
    governorate: str,
    address: str,
    contact_phone: str,
    email: str | None = None,
) -> dict:
    mobile = orders.egyptian_mobile(contact_phone)
    if mobile is not None and mobile not in _numbers_the_customer_gave(ctx):
        # The model writes this argument, and a phone number is the one
        # detail of an order nobody can check by reading it: a transposed
        # digit looks exactly like the real thing, and the parcel goes to a
        # courier who cannot reach anyone. So it has to be a number the
        # customer actually gave -- typed in this conversation, saved on their
        # profile from an earlier order, or the WhatsApp number they are
        # writing from.
        return {
            "error": "phone_not_given",
            "contact_phone": contact_phone,
            "do_instead": "Ask the customer to type the phone number the courier should call, "
            "then confirm with exactly the digits they sent.",
        }
    unconfirmed = _not_yet_agreed(ctx, governorate)
    if unconfirmed is not None:
        return unconfirmed
    result = orders.place_order(
        ctx.session,
        channel=ctx.channel,
        external_id=ctx.external_id,
        customer_name=customer_name,
        governorate=governorate,
        address=address,
        contact_phone=contact_phone,
        email=email,
    )
    if result.get("order_id"):
        # The confirmation itself has already been composed and queued for
        # delivery by `notifications.order_confirmed` -- with the order
        # number, the lines, the shipping fee and the total, none of which a
        # model should be restating from memory. So the turn ends here. It
        # used to run on, and the model's own "تمام، اتأكد الأوردر..." reached
        # the customer as a *second* confirmation for the same order.
        ctx.end_turn = "order_confirmed"
        result["confirmation_sent"] = True
    return result


#: A customer agreeing, in the words they use for it. Read from their own
#: words only (`assistant/customer_words.py`).
_YES = re.compile(
    r"(?<![\w])(?:اه|آه|أه|ااه|ايوه|أيوه|ايوا|أيوا|ايوة|أيوة|تمام|ماشي|اكد|أكد|أكّد|اكده|أكده|"
    r"اكديه|أكديه|موافق|موافقة|تم|يلا|اوك|أوك|اوكي|أوكي|حاضر|اكيد|أكيد|طبعا|طبعًا|كمل|كمّل|نكمل|"
    r"ok|okay|yes|yep|sure|confirm)(?![\w])",
    re.IGNORECASE,
)
_NO = re.compile(r"^\s*(?:لا|لأ|لاء|no)(?![\w])|مش عايز|استنى|لسه", re.IGNORECASE)


def _not_yet_agreed(ctx: ToolContext, governorate: str) -> dict | None:
    """A refusal when the customer has not said yes to a summary, else None.

    instagram, 2026-09-25 21:57:20: order #1042 was placed on «قوله عند النادي
    وهو هيعرف» and a phone number -- an address detail, not an answer. The
    total had been said once, before the name, the address and the phone were
    collected; no summary followed them and nobody agreed to anything. The
    prompt has always asked for «ملخص → موافقة صريحة → confirm_order»; this is
    the tool that refuses when it did not happen.

    The rule: the bot's last message before the customer's latest one states
    the total this order will charge (the same `checkout` get_shipping_fee
    gives), and the customer's latest words agree. A cart that is already
    empty or a governorate with no rate is left to `place_order`, which has its
    own answers for both (`already_confirmed`, `no_rate_set`).
    """
    from assistant import customer_words, reply_facts
    from assistant.tools.catalog_tools import _checkout
    from domain.services import shipping

    resolved = shipping.resolve(ctx.session, governorate)
    fee = shipping.get_fee(ctx.session, resolved) if resolved else None
    checkout = _checkout(ctx, fee) if fee is not None else None
    if checkout is None:
        return None
    total = reply_facts._decimal(str(checkout["total"]))

    history = ctx.history
    last_user = next((i for i in range(len(history) - 1, -1, -1) if history[i].get("role") == USER), None)
    said_before = next(
        (
            (history[i].get("content") or "")
            for i in range(last_user - 1, -1, -1)
            if history[i].get("role") == "assistant" and (history[i].get("content") or "").strip()
        ),
        "",
    ) if last_user is not None else ""
    shown: set = set()
    reply_facts._numbers_in(said_before, shown)
    words = customer_words.latest(history)
    agreed = bool(_YES.search(words)) and not _NO.search(words)
    if total in shown and agreed:
        return None
    return {
        "error": "not_confirmed_by_customer",
        "total": checkout["total"],
        "do_instead": "Send the summary first: the lines, the shipping and the total from "
        "get_shipping_fee's checkout, with the name, address and phone -- then ask «أأكد "
        "الأوردر؟». Call confirm_order only after the customer says yes to that message.",
    }


#: A run of digits the way a number is typed: spaces and dashes allowed
#: inside it, Western or Arabic-Indic digits.
_DIGIT_RUN = re.compile(r"[0-9٠-٩۰-۹][0-9٠-٩۰-۹ \-]{8,}[0-9٠-٩۰-۹]")


def _numbers_the_customer_gave(ctx: ToolContext) -> set[str]:
    """Every mobile number this customer has actually given us, canonical.

    Their own messages in this conversation, the phone saved on their
    profile, and the WhatsApp number they are messaging from -- «نفس الرقم
    ده» is an ordinary answer. Never a number that appears only in the
    model's own words.
    """
    found: set[str] = set()
    for message in ctx.history:
        if message.get("role") != USER:
            continue
        for run in _DIGIT_RUN.findall(message.get("content") or ""):
            mobile = orders.egyptian_mobile(run)
            if mobile:
                found.add(mobile)
    client = identities.client_for(ctx.session, ctx.channel, ctx.external_id)
    for known in (client.phone if client else None, ctx.external_id):
        if known and is_phone_number(known):
            mobile = orders.egyptian_mobile(known)
            if mobile:
                found.add(mobile)
    return found


@tool(
    "get_my_orders",
    "This customer's orders, open ones by default. Call it before any change so you can ask which "
    "order rather than guessing by recency. `modifiable` is computed for you -- do not work it out "
    "from the status yourself.",
    properties={"include_closed": {"type": "boolean", "description": "Include delivered and cancelled."}},
)
def get_my_orders(ctx: ToolContext, include_closed: bool | None = None) -> dict:
    rows = orders.orders_for_identity(
        ctx.session, ctx.channel, ctx.external_id, include_closed=bool(include_closed)
    )
    return {"orders": [orders.order_summary(order) for order in rows]}


@tool(
    "modify_order_quantity",
    "Set one line of an existing order to an absolute quantity, 0-10. 0 removes the line; removing "
    "the last line is refused -- cancel the order instead. Returns the order with a recalculated "
    "total, which you must read back to the customer: on cash on delivery a silently changed "
    "amount becomes an argument at the door. The shipping fee is never re-quoted.",
    properties={
        "order_id": {"type": "string"},
        "variant_id": {"type": "string", "description": "A line already on that order."},
        "quantity": {"type": "integer", "description": "Absolute new quantity, 0-10."},
    },
    required=("order_id", "variant_id", "quantity"),
)
def modify_order_quantity(ctx: ToolContext, order_id: str, variant_id: str, quantity: int) -> dict:
    quantity = int(quantity)
    if quantity < 0 or quantity > 10:
        return {"error": "bad_arguments", "detail": "quantity must be between 0 and 10"}

    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}
    return orders.modify_quantity(ctx.session, order, variant_id, quantity)


@tool(
    "cancel_order",
    "Cancel an order that has not shipped. Returns all its stock and notifies staff. A shipped "
    "order is refused with `already_shipped` and the terms that now apply instead -- read those "
    "numbers back, do not soften them and do not quote a fee of your own.",
    properties={"order_id": {"type": "string"}},
    required=("order_id",),
)
def cancel_order(ctx: ToolContext, order_id: str) -> dict:
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}

    result = orders.cancel(ctx.session, order, by="customer")
    if result.get("error") != "not_modifiable":
        return result
    if order.status == OrderStatus.CANCELLED.value:
        # Nothing to explain: it is already where they want it.
        return result
    # Refused because the courier has it. Saying only "no" is what left the
    # model free to promise a free cancellation the shop cannot honour, or to
    # quote one leg of a fee that is charged both ways.
    code = "already_delivered" if order.status == OrderStatus.DELIVERED.value else "already_shipped"
    return {"error": code, **orders.return_terms(order)}


@tool(
    "get_return_terms",
    "The shop's exchange, return and cancellation terms, with this order's own numbers filled in. "
    "Call it before saying anything about a fee, a deadline or whether something can still be "
    "cancelled -- the terms and the money come from here, never from memory. order_id is optional; "
    "without one you get the general terms and no per-order amount.",
    properties={"order_id": {"type": "string", "description": "Optional; from get_my_orders."}},
)
def get_return_terms(ctx: ToolContext, order_id: str | None = None) -> dict:
    if not order_id:
        return orders.return_terms()
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}
    return orders.return_terms(order)


@tool(
    "add_item_to_order",
    "Add another item to an existing UNSHIPPED order right now -- applied immediately, not "
    "queued for staff. Use this whenever the variant is known and the order has not shipped "
    "('modifiable' from get_my_orders). Merges into the existing line if that variant is "
    "already on the order. Returns the order with its total read back from Shopify itself -- "
    "read that number to the customer; never state a total of your own. If the order has "
    "already shipped, or the customer has not said exactly which piece they want, use "
    "request_item_add instead.",
    properties={
        "order_id": {"type": "string"},
        "variant_id": {"type": "string", "description": "The piece to add."},
        "quantity": {"type": "integer", "description": "How many, 1-10. Defaults to 1."},
    },
    required=("order_id", "variant_id"),
)
def add_item_to_order(
    ctx: ToolContext, order_id: str, variant_id: str, quantity: int | None = None
) -> dict:
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}
    quantity = 1 if quantity is None else int(quantity)
    return orders.add_item(ctx.session, order, variant_id, quantity)


@tool(
    "request_item_swap",
    "Queue a request to REPLACE one item on an order with a different one -- the item named by "
    "from_variant_id comes OFF the order. Only for a customer who wants to change something they "
    "already ordered («بدل», «غيّر المقاس», «ارجعوا دي وابعتوا دي»). If they want a piece IN "
    "ADDITION to what they ordered, use request_item_add instead; filing an addition as a swap "
    "takes a garment they still want off their order. This never applies the swap -- staff check "
    "stock for the replacement and decide. Tell the customer someone will confirm the SWAP; do not "
    "imply it is done. to_variant_id is optional when the customer only described what they want.",
    properties={
        "order_id": {"type": "string"},
        "from_variant_id": {"type": "string", "description": "The line they want to replace."},
        "to_variant_id": {"type": "string", "description": "Optional replacement, if known."},
        "note": {"type": "string", "description": "What the customer said they want."},
    },
    required=("order_id", "from_variant_id"),
)
def request_item_swap(
    ctx: ToolContext,
    order_id: str,
    from_variant_id: str,
    to_variant_id: str | None = None,
    note: str | None = None,
) -> dict:
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}

    line = next((item for item in order.items if item.variant_id == from_variant_id), None)
    if line is None:
        return {"error": "line_not_found", "variant_id": from_variant_id}

    to_variant = ctx.session.get(Variant, to_variant_id) if to_variant_id else None
    summary = f"{order.order_id}: swap {line.product_name} ({line.color}, {line.size})"
    if to_variant is not None:
        summary += f" → {to_variant.product.name} ({to_variant.color}, {to_variant.size})"
    elif note:
        summary += f" → {note}"

    request_id = item_swap_requested(
        ctx.session,
        order,
        {
            "channel": ctx.channel,
            "external_id": ctx.external_id,
            "from_variant_id": from_variant_id,
            "to_variant_id": to_variant_id,
            "note": note,
        },
        summary,
    )
    return {"queued": True, "request_id": request_id, "filed": "item_swap"}


@tool(
    "request_item_add",
    "Queue a request to ADD another item to an existing order, leaving everything already on it "
    "alone. Use this whenever the customer wants a piece *as well as* what they already ordered "
    "-- «ضيفه على نفس الأوردر», «عايز كمان...», «ينفع أزود». Never request_item_swap for this: a "
    "swap takes an item OFF the order. This never applies the addition -- staff check stock and "
    "decide. Tell the customer someone will confirm the ADDITION; do not say anything about "
    "replacing or swapping, and do not imply it is done. to_variant_id is optional when the "
    "customer only described what they want.",
    properties={
        "order_id": {"type": "string"},
        "to_variant_id": {"type": "string", "description": "Optional; the piece to add, if known."},
        "quantity": {"type": "integer", "description": "How many, 1-10. Defaults to 1."},
        "note": {"type": "string", "description": "What the customer said they want."},
    },
    required=("order_id",),
)
def request_item_add(
    ctx: ToolContext,
    order_id: str,
    to_variant_id: str | None = None,
    quantity: int | None = None,
    note: str | None = None,
) -> dict:
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}

    quantity = 1 if quantity is None else int(quantity)
    if quantity < 1 or quantity > 10:
        return {"error": "bad_arguments", "detail": "quantity must be between 1 and 10"}

    addition = ctx.session.get(Variant, to_variant_id) if to_variant_id else None
    if to_variant_id and addition is None:
        return {"error": "variant_not_found", "variant_id": to_variant_id}

    summary = f"{order.order_id}: add "
    if addition is not None:
        summary += f"{addition.product.name} ({addition.color}, {addition.size})"
    else:
        summary += note or "an item the customer described"
    if quantity != 1:
        summary += f" ×{quantity}"

    request_id = item_add_requested(
        ctx.session,
        order,
        {
            "channel": ctx.channel,
            "external_id": ctx.external_id,
            "to_variant_id": to_variant_id,
            "quantity": quantity,
            "note": note,
        },
        summary,
    )
    # What was actually filed, in the words the reply has to match. The turn
    # is checked against this before it is sent -- see
    # `assistant/order_change_claims.py`.
    return {"queued": True, "request_id": request_id, "filed": "item_add"}


@tool(
    "submit_feedback",
    "Record the customer's star rating for a delivered order. Rating is 1-5; free text is optional "
    "on top. Only a delivered order can be rated, and only once.",
    properties={
        "order_id": {"type": "string"},
        "rating": {"type": "integer", "description": "1-5."},
        "text": {"type": "string"},
    },
    required=("order_id", "rating"),
)
def submit_feedback(ctx: ToolContext, order_id: str, rating: int, text: str | None = None) -> dict:
    order = orders.find_order_for_identity(ctx.session, ctx.channel, ctx.external_id, order_id)
    if order is None:
        return {"error": "order_not_found", "order_id": order_id}
    return orders.submit_feedback(ctx.session, order, int(rating), text)
