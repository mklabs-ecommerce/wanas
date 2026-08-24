"""Order service -- the only place "can this order happen?" gets decided.

Every write below happens inside one transaction. If any part fails, none of
it commits: stock decremented with no order record is an inventory count that
is permanently wrong with nothing to show for it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    MODIFIABLE_STATUSES,
    STATUS_SEQUENCE,
    Client,
    Order,
    OrderFeedback,
    OrderStatus,
    Variant,
    utcnow,
)
from backend.services import (
    carts,
    identities,
    inventory,
    notifications,
    shopify_catalog,
    shopify_orders,
)
from backend.services.ids import next_order_id
from backend.services.shipping import get_fee
from backend.services.shipping import resolve as resolve_governorate
from common.events import after_commit
from common.money import money, to_decimal

log = logging.getLogger("wanas.orders")

REQUIRED_FIELDS = ("customer_name", "governorate", "address", "contact_phone")


class Refusal(Exception):
    """A business refusal, not a crash. Carries the payload the tool returns."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", "refused"))
        self.payload = payload


def order_payload(order: Order) -> dict:
    return {
        "order_id": order.order_id,
        # What the customer is told, and what they will find if they ask staff
        # to look it up. `order_id` stays as the internal reference the tools
        # and the dashboard use; only this one is meant to be said out loud.
        "reference": order.shopify_order_name or order.order_id,
        "status": order.status,
        "payment_method": order.payment_method,
        "items": [
            {
                "variant_id": item.variant_id,
                "product_name": item.product_name,
                "size": item.size,
                "color": item.color,
                "length": item.length,
                "quantity": item.quantity,
                "unit_price": money(item.unit_price),
                "unit_original_price": money(item.unit_original_price),
            }
            for item in order.items
        ],
        "subtotal": money(order.subtotal),
        "discount_amount": money(order.discount_amount),
        "shipping_fee": money(order.shipping_fee),
        "total": money(order.total),
    }


def recompute_totals(order: Order) -> None:
    """Subtotal and total are recomputed; `shipping_fee` is not re-quoted.

    It was copied from the rate table at order time and stays copied, even if
    the table changed since.
    """
    subtotal = sum((to_decimal(i.unit_price) * i.quantity for i in order.items), to_decimal(0))
    order.subtotal = subtotal
    order.total = subtotal - to_decimal(order.discount_amount) + to_decimal(order.shipping_fee)


def _blocked_client(session: Session, channel: str, external_id: str, phone: str) -> bool:
    """Blocked is checked before anything is written, independent of channel.

    Both the linked client and an exact phone match are checked: a blocked
    customer messaging from a fresh WhatsApp identity is the case blocking
    exists for.
    """
    client = identities.client_for(session, channel, external_id)
    if client is not None:
        return client.status == "blocked"
    match = identities.find_matching_client(session, phone=phone)
    return match is not None and match.status == "blocked"


def _client_for_order(
    session: Session,
    channel: str,
    external_id: str,
    *,
    name: str,
    phone: str,
    email: str | None,
    address: str,
    governorate: str,
) -> Client:
    """Create or update the client record, inside the order transaction.

    Never links silently: when the phone or email matches an existing client
    and this identity is not linked yet, a fresh record is created and the
    match is recorded as `pending_link` for the bot to ask about. Declined or
    ignored means two separate records, which is the documented outcome.
    """
    identity = identities.get_or_create(session, channel, external_id)
    client = session.get(Client, identity.client_id) if identity.client_id else None

    if client is None:
        match = identities.find_matching_client(session, phone=phone, email=email)
        client = Client(
            full_name=name,
            phone=phone,
            email=(email or None),
            address=address,
            governorate=governorate,
            has_account=False,
            status="active",
        )
        session.add(client)
        session.flush()
        if match is not None:
            # Recorded before the identity is attached to the fresh record:
            # the pending link is an offer to reuse the *pre-existing* client,
            # and it is the only thing that can ever reach link_client.
            identities.note_pending_link(
                session, identity, match, "email" if (email and match.email == email) else "phone"
            )
        identity.client_id = client.client_id
    else:
        # A confirmed change of address becomes the new default -- the flow
        # shows the saved one and asks, it never silently reuses it.
        client.full_name = name or client.full_name
        client.phone = phone or client.phone
        client.email = email or client.email
        client.address = address or client.address
        client.governorate = governorate or client.governorate

    session.flush()
    return client


def _all_lines_unavailable(session: Session, lines) -> list[dict]:
    """Every line in the cart, described as unavailable.

    Used when Shopify refuses the whole order for stock without naming which
    line ran out. Listing all of them is over-broad, but the alternative is
    picking one at random and telling the customer something specific and
    possibly false.
    """
    out = []
    for line in lines:
        variant = session.get(Variant, line.variant_id)
        out.append(
            {
                "variant_id": line.variant_id,
                "product_name": variant.product.name if variant else None,
                "size": variant.size if variant else None,
                "color": variant.color if variant else None,
                "length": variant.length if variant else None,
                "requested": line.quantity,
                "available": 0,
            }
        )
    return out


def place_order(
    session: Session,
    *,
    channel: str,
    external_id: str,
    customer_name: str,
    governorate: str,
    address: str,
    contact_phone: str,
    email: str | None = None,
) -> dict:
    """The atomic transaction. Returns the order payload or a refusal dict."""

    fields = {
        "customer_name": customer_name,
        "governorate": governorate,
        "address": address,
        "contact_phone": contact_phone,
    }
    missing = [name for name in REQUIRED_FIELDS if not (fields[name] or "").strip()]
    if missing:
        # It does not fill in blanks, and it does not infer a governorate from
        # the address text -- that is what the shipping fee is priced on.
        return {"error": "missing_fields", "fields": missing}

    lines = carts._lines(session, channel, external_id)
    if not lines:
        return {"error": "cart_empty"}

    resolved = resolve_governorate(session, governorate)
    if resolved is None:
        # Not in the list at all. There is no separate code for this in the
        # confirm_order contract, and it is literally true that no rate is
        # set, so the documented refusal is used with the valid list attached
        # so the model can re-ask instead of guessing.
        from backend.services.shipping import valid_governorates

        return {
            "error": "no_rate_set",
            "governorate": governorate,
            "known_governorate": False,
            "valid": valid_governorates(session),
        }

    fee = get_fee(session, resolved)
    if fee is None:
        return {"error": "no_rate_set", "governorate": resolved, "known_governorate": True}

    if _blocked_client(session, channel, external_id, contact_phone):
        return {"error": "client_blocked"}

    #: Ties the Shopify order back to this conversation before the local order
    #: exists. `order_id` is only allocated inside the transaction below, and
    #: by then Shopify has already been called.
    reference = f"{channel}:{external_id}"

    # One read of the shelf for the whole order, deliberately outside the
    # per-turn snapshot. Minutes may have passed since "add it to the cart",
    # which is long enough for the storefront to sell the last one -- taking
    # money on a number read at the start of the conversation is exactly the
    # oversell this is here to stop.
    try:
        live = shopify_catalog.fetch_skus([line.variant_id for line in lines])
    except (shopify_catalog.ShopifyUnavailable, shopify_catalog.ShopifyConfigError) as exc:
        # Refusing is the agreed behaviour. An order written locally while
        # Shopify does not know about it is stock sold twice, and the customer
        # only finds out when nothing arrives.
        log.warning("Refusing an order: Shopify unreachable (%s)", exc)
        return {"error": "store_unavailable"}

    # A read-only pass first. Shopify checks the stock again for real when it
    # creates the order, and refuses if it is short -- but its refusal names
    # variants, not sizes and colours, and cannot say "the olive is gone but
    # the black is here". Doing the obvious check ourselves is what lets the
    # bot answer with alternatives instead of an apology.
    out_of_stock: list[dict] = []
    blocked = False
    for line in lines:
        variant = session.get(Variant, line.variant_id)
        if variant is None:
            out_of_stock.append(
                {"variant_id": line.variant_id, "requested": line.quantity, "available": 0}
            )
            continue

        found = live.get(line.variant_id)
        if found is None:
            # On no shelf we can see: no SKU, or removed from the store. Not
            # an empty shelf, and saying "sold out" about it would be a guess
            # dressed as a fact.
            log.warning("%s is not on Shopify; refusing the order", line.variant_id)
            blocked = True
            continue

        available = found.stock_qty if found.product_active else 0
        if found.tracked and available < line.quantity:
            out_of_stock.append(
                {
                    "variant_id": variant.variant_id,
                    "product_name": variant.product.name,
                    "size": variant.size,
                    "color": variant.color,
                    "length": variant.length,
                    "requested": line.quantity,
                    "available": available,
                }
            )

    if blocked:
        return {"error": "store_unavailable"}
    if out_of_stock:
        return {"error": "items_out_of_stock", "items": out_of_stock}

    # Shopify creates the sale and takes the stock in the same call. Done
    # before the local write, not after: a Shopify order with no local row is
    # visible in the admin and fixable, while a local row Shopify never heard
    # of is stock that quietly sells twice.
    try:
        remote = shopify_orders.create_order(
            reference=reference,
            items=[
                {
                    "shopify_variant_id": live[line.variant_id].shopify_id,
                    "quantity": line.quantity,
                    "unit_price": live[line.variant_id].price,
                }
                for line in lines
            ],
            customer_name=customer_name.strip(),
            phone=contact_phone.strip(),
            email=(email or "").strip().lower() or None,
            address=address.strip(),
            governorate=resolved,
            shipping_fee=fee,
        )
    except shopify_orders.OrderRejected as exc:
        if exc.is_out_of_stock:
            # Someone took it between our check a moment ago and this call.
            # Shopify will not tell us which line, so the honest answer is the
            # whole cart rather than a guess.
            log.info("Shopify refused order %s: out of stock (%s)", reference, exc)
            return {"error": "items_out_of_stock", "items": _all_lines_unavailable(session, lines)}
        log.error("Shopify refused order %s: %s", reference, exc)
        return {"error": "store_unavailable"}
    except (shopify_catalog.ShopifyUnavailable, shopify_catalog.ShopifyConfigError) as exc:
        log.warning("Refusing order %s: Shopify unreachable (%s)", reference, exc)
        return {"error": "store_unavailable"}

    committed = False
    nested = session.begin_nested()
    try:
        decremented: list[tuple[str, int]] = [
            (line.variant_id, line.quantity) for line in lines
        ]
        for variant_id, quantity in decremented:
            # Bookkeeping only. Shopify already took these off the shelf as
            # part of creating the order, so writing to it again here would
            # sell every item twice.
            inventory.record_sold(session, variant_id, quantity)

        client = _client_for_order(
            session,
            channel,
            external_id,
            name=customer_name.strip(),
            phone=contact_phone.strip(),
            email=(email or "").strip().lower() or None,
            address=address.strip(),
            governorate=resolved,
        )

        order = Order(
            order_id=next_order_id(session),
            client_id=client.client_id,
            source_channel=channel,
            # Who placed it, so confirmations and status pushes go back down
            # the same DM thread instead of to a phone that may never have
            # used WhatsApp.
            source_external_id=external_id,
            shipping_address=address.strip(),
            contact_phone=contact_phone.strip(),
            governorate=resolved,
            subtotal=to_decimal(0),
            discount_code_used=None,
            # Discount codes are out of scope this phase, so this step is a
            # no-op and the number is always 0 -- stored, not collapsed away.
            discount_amount=to_decimal(0),
            shipping_fee=to_decimal(fee),
            total=to_decimal(0),
            # Cash on delivery goes straight to Confirmed: there is no
            # gateway, so no `Pending payment` and no timeout job.
            status=OrderStatus.CONFIRMED.value,
            payment_method="cash_on_delivery",
            payment_status="pending",
            confirmed_at=utcnow(),
            modification_log=[],
            shopify_order_id=remote["id"],
            shopify_order_name=remote["name"],
        )
        session.add(order)
        session.flush()

        from backend.models import OrderItem

        for line in lines:
            variant = session.get(Variant, line.variant_id)
            # The price Shopify is charging right now, which is also the price
            # the customer was quoted a moment ago. Reading `variant.price`
            # here would bill them from wanas.db while the conversation
            # promised them Shopify's number -- the exact mismatch the move to
            # live reads was meant to remove.
            priced = live.get(line.variant_id)
            unit_price = priced.price if priced else variant.price
            unit_original = priced.original_price if priced else variant.original_price
            session.add(
                OrderItem(
                    order_id=order.order_id,
                    variant_id=variant.variant_id,
                    # Copied, never looked up later: reading today's price for
                    # last month's purchase turns every price change into a
                    # silent rewrite of order history.
                    product_name=variant.product.name,
                    size=variant.size,
                    color=variant.color,
                    length=variant.length,
                    quantity=line.quantity,
                    unit_price=to_decimal(unit_price),
                    unit_original_price=to_decimal(unit_original),
                )
            )
        session.flush()
        session.refresh(order)
        recompute_totals(order)

        carts.clear(session, channel, external_id)
        session.flush()
        nested.commit()
        committed = True
    except Refusal as refusal:
        nested.rollback()
        return refusal.payload
    except Exception:
        nested.rollback()
        raise
    finally:
        if not committed:
            # The order exists on Shopify and its stock is gone, but the local
            # write did not land. Cancelling puts both back. Nothing here can
            # raise -- the failure that got us here is the one worth seeing.
            shopify_orders.try_cancel(remote["id"], reason="OTHER")

    # Threshold checks and notifications happen after the order is safely
    # written, never before.
    for variant_id, _qty in decremented:
        variant = session.get(Variant, variant_id)
        if variant is not None and inventory.breached_threshold(session, variant_id):
            notifications.low_stock_breach(session, variant)

    notifications.order_confirmed(session, order)
    return order_payload(order)


# --------------------------------------------------------------------------
# After the order
# --------------------------------------------------------------------------


def orders_for_identity(session: Session, channel: str, external_id: str, *, include_closed: bool = False):
    client = identities.client_for(session, channel, external_id)
    if client is None:
        return []
    stmt = select(Order).where(Order.client_id == client.client_id)
    if not include_closed:
        stmt = stmt.where(Order.status.in_(list(MODIFIABLE_STATUSES) + [OrderStatus.SHIPPED.value]))
    return list(session.scalars(stmt.order_by(Order.placed_at.desc())).all())


def order_summary(order: Order) -> dict:
    return {
        "order_id": order.order_id,
        "reference": order.shopify_order_name or order.order_id,
        "status": order.status,
        "placed_at": order.placed_at.isoformat() if order.placed_at else None,
        "total": money(order.total),
        # Computed here, not derived by the model from `status`. One place
        # owns the rule.
        "modifiable": order.modifiable,
        "items": [
            {
                "variant_id": item.variant_id,
                "product_name": item.product_name,
                "size": item.size,
                "color": item.color,
                "length": item.length,
                "quantity": item.quantity,
            }
            for item in order.items
        ],
    }


def find_order_for_identity(session: Session, channel: str, external_id: str, order_id: str) -> Order | None:
    """Scoped to this identity's client, so a confused or manipulated model
    cannot read or change another customer's order."""
    client = identities.client_for(session, channel, external_id)
    if client is None:
        return None
    return session.scalar(
        select(Order).where(Order.order_id == order_id, Order.client_id == client.client_id)
    )


def modify_quantity(session: Session, order: Order, variant_id: str, quantity: int) -> dict:
    if not order.modifiable:
        return {"error": "not_modifiable", "status": order.status}

    item = next((i for i in order.items if i.variant_id == variant_id), None)
    if item is None:
        return {"error": "line_not_found", "variant_id": variant_id}

    if len(order.items) == 1 and quantity == 0:
        # An order with no lines is a ghost. Cancelling says what happened.
        return {"error": "would_empty_order", "order_id": order.order_id}

    previous = item.quantity
    delta = quantity - previous

    receipts: list[dict] = []
    edited_on_shopify = False
    nested = session.begin_nested()
    try:
        if order.shopify_order_id:
            # Shopify holds the order and the stock, so the edit goes there and
            # the local row just follows. An increase can still be refused for
            # stock -- Shopify checks as part of committing the edit.
            if delta > 0:
                available = inventory.available_now(variant_id)
                if available is None:
                    raise Refusal({"error": "store_unavailable"})
                if available < delta:
                    raise Refusal({"error": "insufficient_stock", "available": available})
            try:
                shopify_orders.set_line_quantity(
                    order.shopify_order_id,
                    variant_id,
                    quantity,
                    note=f"{order.order_id}: {previous} → {quantity}",
                )
                edited_on_shopify = True
            except shopify_orders.OrderRejected as exc:
                if exc.is_out_of_stock:
                    raise Refusal({"error": "insufficient_stock", "available": 0}) from exc
                log.error("Shopify refused the edit to %s: %s", order.order_id, exc)
                raise Refusal({"error": "store_unavailable"}) from exc
            except (
                shopify_catalog.ShopifyUnavailable,
                shopify_catalog.ShopifyConfigError,
            ) as exc:
                log.warning("Could not edit %s on Shopify: %s", order.order_id, exc)
                raise Refusal({"error": "store_unavailable"}) from exc

            if delta > 0:
                inventory.record_sold(session, variant_id, delta)
            else:
                inventory.record_returned(session, variant_id, -delta)

        elif delta > 0:
            # No Shopify order behind this one -- placed before the move. The
            # old path still applies: claim the stock ourselves.
            result = inventory.decrement(session, variant_id, delta, order_ref=order.order_id)
            if not result.ok:
                if result.reason in ("store_unavailable", "not_on_shopify"):
                    raise Refusal({"error": "store_unavailable"})
                raise Refusal({"error": "insufficient_stock", "available": result.available})
            if result.receipt:
                receipts.append(result.receipt)
        elif delta < 0:
            inventory.release(session, variant_id, -delta, order_ref=order.order_id)

        if quantity == 0:
            session.delete(item)
            session.flush()
            session.refresh(order)
        else:
            item.quantity = quantity
            session.flush()

        recompute_totals(order)
        order.modification_log = list(order.modification_log or []) + [
            {
                "at": utcnow().isoformat(),
                "channel": order.source_channel,
                "change": "quantity",
                "variant_id": variant_id,
                "from": previous,
                "to": quantity,
                "new_total": money(order.total),
            }
        ]
        session.flush()
        nested.commit()
        receipts.clear()
        edited_on_shopify = False  # committed; nothing to undo
    except Refusal as refusal:
        nested.rollback()
        return refusal.payload
    except Exception:
        nested.rollback()
        raise
    finally:
        inventory.return_receipts(receipts, order.order_id)
        if edited_on_shopify:
            # The edit landed on Shopify and the local write did not. Put the
            # quantity back so the two sides say the same thing; if this fails
            # too, the log is the only trace and it names the order.
            try:
                shopify_orders.set_line_quantity(
                    order.shopify_order_id,
                    variant_id,
                    previous,
                    note=f"{order.order_id}: reverting, local write failed",
                )
            except Exception as exc:  # pragma: no cover - compensating path
                log.error(
                    "Shopify order %s is at quantity %s but the local order is not. "
                    "Fix by hand. %s",
                    order.shopify_order_name or order.shopify_order_id,
                    quantity,
                    exc,
                )

    if delta > 0 and inventory.breached_threshold(session, variant_id):
        variant = session.get(Variant, variant_id)
        if variant is not None:
            notifications.low_stock_breach(session, variant)

    notifications.order_modified(session, order, f"{variant_id} {previous} → {quantity}")
    return order_payload(order)


def cancel(
    session: Session,
    order: Order,
    *,
    by: str = "customer",
    notify_customer: bool = False,
    remote_already_cancelled: bool = False,
) -> dict:
    """Close an order and give its stock back.

    `remote_already_cancelled` is set when Shopify is the one telling *us* --
    staff cancelled in the admin and the webhook arrived. Asking Shopify to
    cancel an order it has already cancelled is a call that can only fail, and
    the error it logs looks like a bug rather than the no-op it is.
    """
    if not order.modifiable:
        return {"error": "not_modifiable", "status": order.status}

    if order.shopify_order_id and remote_already_cancelled:
        # Shopify restocked as part of its own cancel. The local rows still
        # have to stop claiming these units are sold.
        for item in order.items:
            inventory.record_returned(session, item.variant_id, item.quantity)
    elif order.shopify_order_id:
        # Shopify owns the stock for this order, so it has to be the one to
        # give it back -- `restock=True` on the cancel. Doing it locally as
        # well would put every item back twice.
        try:
            shopify_orders.cancel_order(
                order.shopify_order_id,
                reason="CUSTOMER" if by == "customer" else "OTHER",
            )
        except (
            shopify_orders.OrderRejected,
            shopify_catalog.ShopifyUnavailable,
            shopify_catalog.ShopifyConfigError,
        ) as exc:
            # A cancellation the customer has already asked for should not
            # fail because the store is briefly unreachable, so the local
            # order is still closed. The order stays open in the admin, which
            # is visible and fixable; the log names it.
            log.error(
                "Could not cancel Shopify order %s (%s) -- cancel it by hand: %s",
                order.shopify_order_name or order.shopify_order_id,
                order.order_id,
                exc,
            )
        # Whether or not the call landed, the local row has to stop claiming
        # these units are sold. If the cancel failed, the next
        # `shopify_check_live` run reports the disagreement rather than it
        # sitting silently.
        for item in order.items:
            inventory.record_returned(session, item.variant_id, item.quantity)
    else:
        # Placed before the move, so no Shopify order exists and the local row
        # is the only record of the stock.
        for item in order.items:
            inventory.release(session, item.variant_id, item.quantity, order_ref=order.order_id)

    order.status = OrderStatus.CANCELLED.value
    order.cancelled_at = utcnow()
    order.modification_log = list(order.modification_log or []) + [
        {"at": utcnow().isoformat(), "channel": order.source_channel, "change": "cancelled", "by": by}
    ]
    session.flush()

    notifications.order_cancelled(session, order, by=by)
    if notify_customer:
        after_commit(session, lambda: notifications.order_status_changed(session, order))
    return {"order_id": order.order_id, "status": order.status}


def apply_swap(session: Session, order: Order, from_variant_id: str, to_variant_id: str) -> dict:
    """Staff approving a swap from the review queue.

    Runs the same "apply automatically" logic as a quantity change: the
    replacement goes through the atomic decrement exactly like a new order,
    and the original is released only once that succeeds. Staff decided *what*
    to swap; whether the stock is there is still the database's call.
    """
    if not order.modifiable:
        return {"error": "not_modifiable", "status": order.status}

    item = next((i for i in order.items if i.variant_id == from_variant_id), None)
    if item is None:
        return {"error": "line_not_found", "variant_id": from_variant_id}

    replacement = session.get(Variant, to_variant_id)
    if replacement is None:
        return {"error": "variant_not_found", "variant_id": to_variant_id}

    receipts: list[dict] = []
    quantity = item.quantity
    nested = session.begin_nested()
    try:
        if order.shopify_order_id:
            replacement_live = shopify_catalog.try_fetch_one(to_variant_id)
            if replacement_live is None:
                raise Refusal({"error": "store_unavailable"})
            if replacement_live.tracked and replacement_live.stock_qty < quantity:
                raise Refusal(
                    {"error": "insufficient_stock", "available": replacement_live.stock_qty}
                )
            try:
                shopify_orders.swap_line(
                    order.shopify_order_id,
                    from_variant_id,
                    replacement_live.shopify_id,
                    quantity,
                    note=f"{order.order_id}: {from_variant_id} → {to_variant_id}",
                )
            except shopify_orders.OrderRejected as exc:
                if exc.is_out_of_stock:
                    raise Refusal({"error": "insufficient_stock", "available": 0}) from exc
                log.error("Shopify refused the swap on %s: %s", order.order_id, exc)
                raise Refusal({"error": "store_unavailable"}) from exc
            except (
                shopify_catalog.ShopifyUnavailable,
                shopify_catalog.ShopifyConfigError,
            ) as exc:
                log.warning("Could not swap on %s: %s", order.order_id, exc)
                raise Refusal({"error": "store_unavailable"}) from exc

            # Shopify moved the stock as part of the edit; these only keep the
            # local rows readable.
            inventory.record_sold(session, to_variant_id, quantity)
            inventory.record_returned(session, from_variant_id, quantity)
        else:
            result = inventory.decrement(
                session, to_variant_id, quantity, order_ref=order.order_id
            )
            if not result.ok:
                if result.reason in ("store_unavailable", "not_on_shopify"):
                    raise Refusal({"error": "store_unavailable"})
                raise Refusal({"error": "insufficient_stock", "available": result.available})
            if result.receipt:
                receipts.append(result.receipt)
            inventory.release(session, from_variant_id, quantity, order_ref=order.order_id)

        item.variant_id = replacement.variant_id
        # Re-snapshot: the packing slip has to describe what is actually being
        # sent, at the price it is being sent for.
        item.product_name = replacement.product.name
        item.size = replacement.size
        item.color = replacement.color
        item.length = replacement.length
        # Shopify's price for the replacement, for the same reason a new order
        # uses it: the packing slip has to match what the shop is charging.
        swapped = shopify_catalog.try_fetch_one(to_variant_id)
        item.unit_price = to_decimal(swapped.price if swapped else replacement.price)
        item.unit_original_price = to_decimal(
            swapped.original_price if swapped else replacement.original_price
        )
        session.flush()

        recompute_totals(order)
        order.modification_log = list(order.modification_log or []) + [
            {
                "at": utcnow().isoformat(),
                "channel": "dashboard",
                "change": "item_swap",
                "from": from_variant_id,
                "to": to_variant_id,
                "new_total": money(order.total),
            }
        ]
        session.flush()
        nested.commit()
        receipts.clear()
    except Refusal as refusal:
        nested.rollback()
        return refusal.payload
    except Exception:
        nested.rollback()
        raise
    finally:
        inventory.return_receipts(receipts, order.order_id)

    if inventory.breached_threshold(session, to_variant_id):
        notifications.low_stock_breach(session, replacement)
    notifications.order_modified(session, order, f"swap {from_variant_id} → {to_variant_id}")
    return order_payload(order)


def advance_status(session: Session, order: Order, new_status: str) -> dict:
    """Staff moving an order along. Forward only, one stage at a time, so a
    tracking push cannot describe a stage that was skipped."""
    if order.status == OrderStatus.CANCELLED.value:
        return {"error": "not_modifiable", "status": order.status}
    if new_status not in STATUS_SEQUENCE:
        return {"error": "bad_status", "status": new_status}
    current_index = STATUS_SEQUENCE.index(order.status) if order.status in STATUS_SEQUENCE else -1
    if STATUS_SEQUENCE.index(new_status) != current_index + 1:
        return {"error": "bad_transition", "from": order.status, "to": new_status}

    order.status = new_status
    stamp = {
        OrderStatus.PACKED.value: "packed_at",
        OrderStatus.SHIPPED.value: "shipped_at",
        OrderStatus.DELIVERED.value: "delivered_at",
    }.get(new_status)
    if stamp:
        setattr(order, stamp, utcnow())
    if new_status == OrderStatus.DELIVERED.value:
        # Cash on delivery settles when the courier hands it over.
        order.payment_status = "paid"
    session.flush()

    # The push (and the feedback request on Delivered) go out only once the
    # status change has committed. `new_status` is bound now, not read from the
    # order later: a Shopify fulfilment walks Confirmed -> Packed -> Shipped
    # inside one transaction, and a callback that reads `order.status` at
    # commit time would send "shipped" twice and never send "packed".
    stage = new_status
    after_commit(session, lambda: notifications.order_status_changed(session, order, stage))
    return {"order_id": order.order_id, "status": order.status}


def submit_feedback(session: Session, order: Order, rating: int, text: str | None) -> dict:
    if order.status != OrderStatus.DELIVERED.value:
        return {"error": "not_delivered", "status": order.status}
    if session.scalar(select(OrderFeedback).where(OrderFeedback.order_id == order.order_id)):
        return {"error": "already_rated", "order_id": order.order_id}
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        return {"error": "bad_arguments", "detail": "rating must be an integer from 1 to 5"}

    feedback = OrderFeedback(
        order_id=order.order_id,
        client_id=order.client_id,
        rating=rating,
        text=(text or None),
    )
    session.add(feedback)
    session.flush()
    return {"saved": True, "order_id": order.order_id, "rating": rating}
