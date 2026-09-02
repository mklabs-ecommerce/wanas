"""Creating the sale on Shopify itself.

Until now a WhatsApp order only took stock off the shelf; the order lived in
`wanas.db` and the admin never heard about it. This puts the sale where the
storefront's sales are, so Orders in the admin is the whole business rather
than the half that came through the website.

Two consequences worth stating, because they change how the rest of the order
path has to behave:

**Shopify decrements the inventory.** `orderCreate` with
`inventoryBehaviour: DECREMENT_OBLIGATORY` takes the stock as part of creating
the order, and refuses the whole order if there is not enough. That replaces
the manual reserve in `shopify_inventory` for this path -- doing both would
take every item off the shelf twice.

**There is no transaction across the two systems.** The order is created on
Shopify first and written locally second, because the failure that leaves the
least mess is a Shopify order with no local row: it is visible in the admin,
the stock is correctly gone, and staff can see it. The reverse -- a local order
Shopify has never heard of -- is invisible stock that quietly oversells. When
the local write fails the Shopify order is cancelled to compensate.
"""

from __future__ import annotations

import logging

from integrations.shopify.client import (  # noqa: F401  (re-exported)
    ShopifyConfigError,
    ShopifyUnavailable,
    get_client,
)

log = logging.getLogger("wanas.shopify.orders")

#: Every order this service creates carries these. `chatbot` is the one that
#: matters operationally -- it is what lets staff filter the admin down to
#: "orders a person never typed" when something looks wrong.
ORDER_TAGS = ("chatbot", "cash-on-delivery")

#: The channel tag, which is the *other* half of what the admin's Channel
#: column can tell staff: "Chatbot Integration" says a person never typed the
#: order, and this says which conversation they typed it in.
#:
#: It used to be the literal string `whatsapp` on every order the bot placed,
#: Instagram sales included -- which made the admin quietly disagree with the
#: dashboard about where half the sales came from. Orders created before this
#: still carry the wrong tag; `Order.source_channel` in Postgres has always
#: been right, and is what the dashboard reads first.
CHANNEL_TAGS = {"whatsapp": "whatsapp", "instagram_dm": "instagram"}
DEFAULT_CHANNEL_TAG = "whatsapp"


#: How the order note names the channel. The note is what a delivery driver
#: and a member of staff read on the packing slip, so an Instagram sale saying
#: "WhatsApp order" is a small lie in the one place nobody checks.
CHANNEL_NOTES = {"whatsapp": "WhatsApp", "instagram_dm": "Instagram"}


def channel_tag(channel: str | None) -> str:
    return CHANNEL_TAGS.get(channel or "", DEFAULT_CHANNEL_TAG)

#: Shown in the admin next to the order instead of "Online Store".
SOURCE_NAME = "wanas-chatbot"


class OrderRejected(RuntimeError):
    """Shopify refused to create the order and said why.

    Carries the raw userErrors so the caller can tell an out-of-stock refusal
    (the customer's problem, and answerable) from a malformed request (ours,
    and not something to blame on the shelf).
    """

    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []

    @property
    def is_out_of_stock(self) -> bool:
        blob = " ".join(
            f"{e.get('code', '')} {e.get('message', '')}" for e in self.errors
        ).lower()
        return any(
            phrase in blob
            for phrase in ("insufficient", "out of stock", "not enough", "unavailable_quantity")
        )


ORDER_CREATE = """
mutation($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    order {
      id
      name
      displayFinancialStatus
      totalPriceSet { shopMoney { amount currencyCode } }
    }
    userErrors { field message }
  }
}
"""

ORDER_CANCEL = """
mutation($orderId: ID!, $reason: OrderCancelReason!, $refund: Boolean!,
         $restock: Boolean!, $notify: Boolean) {
  orderCancel(orderId: $orderId, reason: $reason, refund: $refund,
              restock: $restock, notifyCustomer: $notify) {
    job { id }
    orderCancelUserErrors { field message code }
  }
}
"""


def normalise_phone(raw: str | None) -> str | None:
    """Egyptian mobile numbers in the form Shopify accepts.

    Customers type `01067177128`, which is how everyone in Egypt writes their
    number and what WhatsApp needs. Shopify checks the phone against the
    address country and rejects the whole order with "Phone is invalid" -- so
    an untranslated number does not degrade the order, it destroys it.

    Anything that is not recognisably an Egyptian mobile is returned as None
    rather than guessed at. A wrong number on a cash-on-delivery order is a
    parcel nobody can deliver.
    """
    if not raw:
        return None

    digits = "".join(ch for ch in str(raw) if ch.isdigit())

    # 01X XXXX XXXX -- the local form, 11 digits.
    if len(digits) == 11 and digits.startswith("01"):
        return "+20" + digits[1:]
    # 201X XXXX XXXX -- already international, missing the plus.
    if len(digits) == 12 and digits.startswith("201"):
        return "+" + digits
    # 1X XXXX XXXX -- the leading zero dropped somewhere along the way.
    if len(digits) == 10 and digits.startswith("1"):
        return "+20" + digits

    log.warning("Could not read %r as an Egyptian mobile number", raw)
    return None


def _line(item: dict) -> dict:
    """One order line, priced from what we are actually charging.

    `priceSet` is sent explicitly rather than letting Shopify look the variant
    up. The customer agreed to a number in the conversation, and an order that
    silently re-prices itself from the catalog between the promise and the
    record is the same broken trade the live-price work removed elsewhere.
    """
    return {
        "variantId": item["shopify_variant_id"],
        "quantity": int(item["quantity"]),
        "priceSet": {
            "shopMoney": {"amount": f"{item['unit_price']:.2f}", "currencyCode": "EGP"}
        },
        "requiresShipping": True,
    }


def _split_name(name: str) -> tuple[str, str]:
    """The first/last split Shopify wants, done once for both places it is
    needed -- the address and the customer record -- so the packing slip and
    the Customers list can never disagree about the same person.

    Egyptian names routinely run to four words and splitting on the first
    space would file "حازم عبد الحميد" under a surname of "عبد الحميد
    عبدالحميد". Everything after the first word goes in `lastName` --
    imperfect, but it keeps the full name intact.
    """
    parts = (name or "").strip().split(maxsplit=1)
    first = parts[0] if parts else name
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def _find_customer_gid(phone: str | None) -> str | None:
    """An existing Shopify customer id for this phone, best effort.

    Wrapped in the widest possible except on purpose. This runs inside the
    order path, and nothing it can tell us is worth losing a sale over: with
    no id the order is still created, it just reaches the admin without a
    customer attached, which is exactly what
    `scripts/shopify_backfill_customers.py` exists to repair.
    """
    if not phone:
        return None
    try:
        from integrations.shopify import admin_customers

        found = admin_customers.find_customer(phone=phone)
    except Exception:
        log.warning("could not look up a Shopify customer for the order", exc_info=True)
        return None
    gid = (found or {}).get("id")
    return gid if isinstance(gid, str) and gid.strip() else None


def _customer(
    *, name: str, phone: str | None, email: str | None, customer_gid: str | None = None
) -> dict | None:
    """The customer to attach to the order, or None when there is nothing to
    attach them by.

    Without this the order reaches the admin with a shipping address and no
    customer, which is what the Orders list renders as "No customer" -- and
    what leaves `numberOfOrders` empty, so nothing downstream can tell a
    returning buyer from a first-time one.

    **A phone is not something `toUpsert` can be keyed on.** This block used
    to be sent with a name and a phone and nothing else, on the reading that
    Shopify would match or create a customer from the phone the way it does
    from an email. It does not:

        OrderCreateUpsertCustomerAttributesInput requires at least one of
        id, email

    and it refuses the *whole* `orderCreate`. This shop is cash on delivery
    and never asks for an email, so that refusal was not an edge case -- it
    was every order. The bot collected a name, an address and a phone, said
    "تأكيد الأوردر؟", and then could not place it; `confirm_order` returned an
    error and the turn escalated to a person, which is what a customer saw as
    "someone from the team will contact you".

    So the block is built only when there is a key Shopify accepts: an `id`
    resolved from the phone (`_find_customer_gid`), or an email. With
    neither, it is omitted and the order is created without a customer --
    strictly better than an order that does not exist. The phone still rides
    along as an attribute, on the address, and in the note.

    The name given for *this* order overwrites the name on a matched record.
    That is the intended reading -- it is the name the customer typed most
    recently -- and the per-order name is on the shipping address either way.
    """
    if not customer_gid and not email:
        return None

    first, last = _split_name(name)
    attributes: dict[str, str] = {}
    if customer_gid:
        attributes["id"] = customer_gid
    if first:
        attributes["firstName"] = first
    if last:
        attributes["lastName"] = last
    if phone:
        attributes["phone"] = phone
    if email:
        attributes["email"] = email
    return {"toUpsert": attributes}


def _is_customer_error(errors: list[dict] | None) -> bool:
    """Whether Shopify's refusal named the customer block -- for the log line
    only, deliberately not for deciding whether to retry.

    A phone that already belongs to a different record is a refusal the order
    survives without its customer, but which `field` path Shopify reports that
    in is not documented and not something to bet a sale on. `create_order`
    drops the block on any refusal that is not an empty shelf; this only says
    whether the message explained itself.
    """
    for error in errors or []:
        field = error.get("field") or []
        if any("customer" in str(part).lower() for part in field):
            return True
    return False


def _address(*, name: str, phone: str, address: str, governorate: str) -> dict:
    first, last = _split_name(name)
    out = {
        "firstName": first,
        "lastName": last,
        "address1": address,
        "city": governorate,
        "province": governorate,
        "countryCode": "EG",
    }
    # Omitted rather than sent in a form Shopify will reject. The number is
    # still on the order note and in wanas.db, so nothing is lost -- but a
    # rejected order loses the whole sale.
    if phone:
        out["phone"] = phone
    return out


def create_order(
    *,
    reference: str,
    items: list[dict],
    customer_name: str,
    phone: str,
    email: str | None,
    address: str,
    governorate: str,
    shipping_fee,
    note: str | None = None,
    channel: str | None = None,
) -> dict:
    """Create the order on Shopify. Returns {"id", "name"}.

    `items` is [{shopify_variant_id, quantity, unit_price}].

    Raises OrderRejected when Shopify declined, ShopifyUnavailable when the
    call did not complete. The caller must treat those differently: the first
    can mean the shelf emptied, the second never does.
    """
    intl = normalise_phone(phone)
    order = {
        "lineItems": [_line(item) for item in items],
        "shippingAddress": _address(
            name=customer_name, phone=intl, address=address, governorate=governorate
        ),
        "billingAddress": _address(
            name=customer_name, phone=intl, address=address, governorate=governorate
        ),
        "tags": [*ORDER_TAGS, channel_tag(channel)],
        "sourceIdentifier": reference,
        # Cash on delivery: the money has not moved, so the order is
        # deliberately left unpaid rather than marked paid on creation. Marking
        # it paid would make every revenue report in the admin count cash that
        # is still in the customer's pocket.
        "financialStatus": "PENDING",
        "shippingLines": [
            {
                "title": f"Delivery — {governorate}",
                "priceSet": {
                    "shopMoney": {
                        "amount": f"{shipping_fee:.2f}",
                        "currencyCode": "EGP",
                    }
                },
            }
        ],
        # The phone goes in the note as it was given, always. If Shopify would
        # not take it as a field, this is the only place the delivery driver
        # can find it.
        "note": note
        or (
            f"{CHANNEL_NOTES.get(channel or '', 'Chatbot')} order {reference} "
            f"(Wanas chatbot). Cash on delivery. Phone: {phone}"
        ),
    }

    if intl:
        order["phone"] = intl
    if email:
        order["email"] = email

    # Only worth a lookup when there is no email to key on already.
    customer_gid = None if email else _find_customer_gid(intl)
    customer = _customer(
        name=customer_name, phone=intl, email=email, customer_gid=customer_gid
    )
    if customer:
        order["customer"] = customer

    options = {
        # The bot already sent a confirmation on WhatsApp. A second one from
        # Shopify would arrive branded "My Store" and read like a different
        # shop confirming an order the customer placed once.
        "sendReceipt": False,
        "sendFulfillmentReceipt": False,
        # Shopify takes the stock as part of creating the order. "Obeying
        # policy" means it honours each variant's own out-of-stock setting --
        # the same rule the storefront checkout follows -- rather than
        # decrementing past zero. `DECREMENT_IGNORING_POLICY` is the other
        # decrementing option and it will happily leave the count negative,
        # which is the oversell this whole change exists to stop.
        "inventoryBehaviour": "DECREMENT_OBEYING_POLICY",
    }

    def send(payload: dict) -> dict:
        data = get_client()(ORDER_CREATE, {"order": payload, "options": options})
        result = data.get("orderCreate") or {}
        errors = result.get("userErrors") or []
        if errors:
            message = "; ".join(e.get("message", "") for e in errors)
            log.warning("Shopify refused to create order %s: %s", reference, message)
            raise OrderRejected(message, errors)
        return result

    try:
        result = send(order)
    except OrderRejected as exc:
        # An empty shelf is never retried: the second call is refused for the
        # same reason, and the customer is owed the alternatives this raises
        # for. Every *other* refusal is retried without the customer block,
        # rather than only the ones whose `field` path names it -- which shape
        # Shopify reports a phone conflict in is not something this code
        # should be betting a sale on. If the refusal was about something
        # else the second call is refused identically and that is what
        # propagates; if it was about the customer in a shape nobody
        # predicted, the sale survives without the link.
        if exc.is_out_of_stock or not customer:
            raise
        log.warning(
            "Shopify refused order %s (%s); retrying without the customer%s",
            reference,
            exc,
            "" if _is_customer_error(exc.errors) else " -- the refusal did not name one",
        )
        order.pop("customer", None)
        result = send(order)

    created = result.get("order") or {}
    if not created.get("id"):
        raise ShopifyUnavailable("Shopify returned no order")

    log.info("Created Shopify order %s for %s", created.get("name"), reference)
    return {"id": created["id"], "name": created.get("name") or ""}


def cancel_order(shopify_order_id: str, *, reason: str = "CUSTOMER", restock: bool = True) -> None:
    """Cancel on Shopify, putting the stock back.

    `restock=True` is what returns the inventory; without it a cancelled order
    leaves its items off the shelf forever. `refund=False` because nothing was
    ever charged -- cash on delivery means there is no payment to reverse, and
    asking Shopify to refund zero is an error, not a no-op.

    **This is asynchronous.** Shopify accepts the cancellation and returns a
    job; the order closing and the stock coming back happen a moment later.
    Returning without an exception means "Shopify agreed to cancel", not "the
    stock is back". Anything that reads the shelf straight afterwards -- a
    verification script, a test against the live store -- has to wait for it.
    """
    data = get_client()(
        ORDER_CANCEL,
        {
            "orderId": shopify_order_id,
            "reason": reason,
            "refund": False,
            "restock": restock,
            # The customer is being told on WhatsApp by whoever cancelled.
            "notify": False,
        },
    )
    result = data.get("orderCancel") or {}
    errors = result.get("orderCancelUserErrors") or []
    if errors:
        message = "; ".join(e.get("message", "") for e in errors)
        log.warning("Shopify refused to cancel %s: %s", shopify_order_id, message)
        raise OrderRejected(message, errors)


#: Settling a cash-on-delivery order. Shopify has no inverse -- there is no
#: "mark as unpaid" mutation -- so this is a one-way door.
ORDER_MARK_AS_PAID = """
mutation($input: OrderMarkAsPaidInput!) {
  orderMarkAsPaid(input: $input) {
    order { id displayFinancialStatus }
    userErrors { field message }
  }
}
"""


def mark_as_paid(shopify_order_id: str) -> dict:
    """Record that a cash-on-delivery order was paid.

    The money moves in the street, not through Shopify, so nothing else can
    ever tell this shop an order settled -- which is why every order the bot
    places sits at PENDING (`create_order` leaves it there on purpose) until
    somebody says the courier handed the cash over.

    Returns the new financial status, or a payload with "error" set when
    Shopify refused -- an already-paid or cancelled order cannot be marked
    paid, and that refusal is passed through as a reason rather than retried
    or reinterpreted here. Raises ShopifyUnavailable / ShopifyConfigError, so
    an outage stays tellable apart from a refusal.
    """
    data = get_client()(ORDER_MARK_AS_PAID, {"input": {"id": shopify_order_id}})
    result = data.get("orderMarkAsPaid") or {}
    errors = result.get("userErrors") or []
    if errors:
        message = "; ".join(e.get("message", "") for e in errors)
        log.warning("Shopify refused to mark %s paid: %s", shopify_order_id, message)
        return {"error": "payment_rejected", "detail": message}

    order = result.get("order") or {}
    return {"id": order.get("id"), "financial_status": order.get("displayFinancialStatus")}


def try_mark_as_paid(shopify_order_id: str) -> bool:
    """`mark_as_paid` that never raises, for the delivery path.

    A local order reaching Delivered has already settled -- cash on delivery
    means the courier collected it -- and that is recorded locally inside the
    transaction. Telling Shopify is a separate, after-commit errand, and a
    Shopify outage must not undo a delivery that happened. A failure is logged
    and the order can still be marked paid by hand from the dashboard.
    """
    try:
        result = mark_as_paid(shopify_order_id)
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        log.warning("Could not mark %s paid on Shopify: %s", shopify_order_id, exc)
        return False
    if "error" in result:
        # Almost always "already paid", which is not worth an error line: the
        # local row and Shopify agree, which is the point of the call.
        log.info("Shopify would not mark %s paid: %s", shopify_order_id, result["detail"])
        return False
    return True


# --------------------------------------------------------------------------
# editing a placed order
# --------------------------------------------------------------------------
#
# Shopify does not let you write a new quantity onto a placed order. You open
# an edit session, change a *calculated* copy, and commit it -- three calls,
# and the ids in the middle step are not the line item ids from the order.
# That is why this is a flow rather than one mutation.

EDIT_BEGIN = """
mutation($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder {
      id
      lineItems(first: 100) {
        nodes { id quantity variant { id sku } }
      }
    }
    userErrors { field message }
  }
}
"""

EDIT_SET_QUANTITY = """
mutation($calculatedOrderId: ID!, $lineItemId: ID!, $quantity: Int!, $restock: Boolean!) {
  orderEditSetQuantity(id: $calculatedOrderId, lineItemId: $lineItemId,
                       quantity: $quantity, restock: $restock) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

EDIT_COMMIT = """
mutation($id: ID!, $notify: Boolean!, $note: String) {
  orderEditCommit(id: $id, notifyCustomer: $notify, staffNote: $note) {
    order { id name }
    userErrors { field message }
  }
}
"""


def _errors_of(block: dict, key: str) -> None:
    errors = (block or {}).get("userErrors") or []
    if errors:
        message = "; ".join(e.get("message", "") for e in errors)
        log.warning("Shopify rejected %s: %s", key, message)
        raise OrderRejected(message, errors)


def set_line_quantity(
    shopify_order_id: str, sku: str, quantity: int, *, note: str | None = None
) -> None:
    """Change one line's quantity on a placed order, by SKU.

    `restock=True` on a decrease is what returns the difference to the shelf;
    an increase takes more off it. Either way Shopify does the inventory, the
    same as it did when the order was created -- the caller must not also
    adjust it.

    Matched on SKU rather than position: the line order in a calculated edit is
    not guaranteed to match the order we sent, and quietly changing the wrong
    line is worse than failing.
    """
    client = get_client()

    begun = client(EDIT_BEGIN, {"id": shopify_order_id}).get("orderEditBegin") or {}
    _errors_of(begun, "orderEditBegin")
    calculated = begun.get("calculatedOrder") or {}
    calculated_id = calculated.get("id")
    if not calculated_id:
        raise ShopifyUnavailable("Shopify opened no edit session")

    line_id = None
    for node in (calculated.get("lineItems") or {}).get("nodes") or []:
        if ((node.get("variant") or {}).get("sku") or "").strip() == sku:
            line_id = node["id"]
            break
    if line_id is None:
        raise OrderRejected(f"{sku} is not a line on that Shopify order")

    result = client(
        EDIT_SET_QUANTITY,
        {
            "calculatedOrderId": calculated_id,
            "lineItemId": line_id,
            "quantity": int(quantity),
            "restock": True,
        },
    ).get("orderEditSetQuantity")
    _errors_of(result, "orderEditSetQuantity")

    committed = client(
        EDIT_COMMIT,
        {
            "id": calculated_id,
            # The bot tells the customer itself, in the language they wrote in.
            "notify": False,
            "note": note or "Changed from the Wanas chatbot",
        },
    ).get("orderEditCommit")
    _errors_of(committed, "orderEditCommit")


EDIT_ADD_VARIANT = """
mutation($id: ID!, $variantId: ID!, $quantity: Int!) {
  orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity,
                      allowDuplicates: true) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""


def swap_line(
    shopify_order_id: str,
    from_sku: str,
    to_variant_id: str,
    quantity: int,
    *,
    note: str | None = None,
) -> None:
    """Replace one line with a different variant, in a single edit session.

    Both halves are committed together on purpose. Two separate edits would
    leave a window where the order has neither item, or both -- and if the
    second failed, an order that no longer matches what the customer agreed to.
    """
    client = get_client()

    begun = client(EDIT_BEGIN, {"id": shopify_order_id}).get("orderEditBegin") or {}
    _errors_of(begun, "orderEditBegin")
    calculated = begun.get("calculatedOrder") or {}
    calculated_id = calculated.get("id")
    if not calculated_id:
        raise ShopifyUnavailable("Shopify opened no edit session")

    line_id = None
    for node in (calculated.get("lineItems") or {}).get("nodes") or []:
        if ((node.get("variant") or {}).get("sku") or "").strip() == from_sku:
            line_id = node["id"]
            break
    if line_id is None:
        raise OrderRejected(f"{from_sku} is not a line on that Shopify order")

    added = client(
        EDIT_ADD_VARIANT,
        {"id": calculated_id, "variantId": to_variant_id, "quantity": int(quantity)},
    ).get("orderEditAddVariant")
    _errors_of(added, "orderEditAddVariant")

    removed = client(
        EDIT_SET_QUANTITY,
        {
            "calculatedOrderId": calculated_id,
            "lineItemId": line_id,
            "quantity": 0,
            "restock": True,
        },
    ).get("orderEditSetQuantity")
    _errors_of(removed, "orderEditSetQuantity")

    committed = client(
        EDIT_COMMIT,
        {"id": calculated_id, "notify": False, "note": note or "Item swap approved by staff"},
    ).get("orderEditCommit")
    _errors_of(committed, "orderEditCommit")


def try_cancel(shopify_order_id: str, *, reason: str = "OTHER") -> bool:
    """Cancel without raising -- the compensating path.

    Used when the local write failed after Shopify accepted the order. Raising
    here would hide the original failure behind a second one, and the original
    is the one worth seeing. A failure is logged with the order name so it can
    be cancelled by hand.
    """
    try:
        cancel_order(shopify_order_id, reason=reason)
        return True
    except (OrderRejected, ShopifyUnavailable, ShopifyConfigError) as exc:
        log.error(
            "ORPHAN: Shopify order %s was created but the local write failed, and "
            "cancelling it failed too. Cancel it by hand in the admin. %s",
            shopify_order_id,
            exc,
        )
        return False
