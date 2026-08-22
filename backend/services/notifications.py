"""Notification service.

The only thing that turns internal events into actual messages, so the Order
and Inventory modules stay about business rules rather than message formatting.

Two destinations: the customer over WhatsApp, and staff via the alert queue
(`StaffQueueItem`). There is no email provider, because the WhatsApp flow
never collects an email.

Outbound delivery is a port, not an import. `LogSender` is the default, so
every proactive message is produced, formatted and observable before any Meta
credential exists; registering the real client at startup is the only change.
This is also what keeps the dependency direction one-way: /backend/ knows the
interface, and never imports /chatbot/.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from backend.config import settings
from backend.events import after_commit
from backend.models import Order, QueueKind, Variant, utcnow
from backend.money import money
from backend.services import identities, queues

log = logging.getLogger("wanas.notifications")

#: Meta only allows free-form, business-initiated text inside this window of
#: the customer's last message; outside it, only a pre-approved template may
#: reopen the conversation (`send_proactive` below).
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)


@dataclass
class OutboundMessage:
    to: str
    text: str
    kind: str = "text"
    image_path: str | None = None
    template: str | None = None
    delivered: bool = True
    error: str | None = None


class OutboundSender(Protocol):
    def send_text(self, to: str, text: str, *, template: str | None = None) -> OutboundMessage: ...

    def send_image(self, to: str, image_path: str, *, caption: str = "") -> OutboundMessage: ...

    def send_template(self, to: str, template: str, *, language: str = "ar") -> OutboundMessage: ...


@dataclass
class LogSender:
    """Default sender: logs, and keeps the last messages for the harness and
    the tests to assert on. Never touches the network."""

    sent: list[OutboundMessage] = field(default_factory=list)

    def send_text(self, to: str, text: str, *, template: str | None = None) -> OutboundMessage:
        message = OutboundMessage(to=to, text=text, template=template)
        self.sent.append(message)
        log.info("[outbound:%s] %s", to, text.replace("\n", " | "))
        return message

    def send_image(self, to: str, image_path: str, *, caption: str = "") -> OutboundMessage:
        message = OutboundMessage(to=to, text=caption, kind="image", image_path=image_path)
        self.sent.append(message)
        log.info("[outbound:%s] image %s", to, image_path)
        return message

    def send_interactive(self, to: str, payload: dict, *, fallback: str = "") -> OutboundMessage:
        message = OutboundMessage(to=to, text=payload.get("body") or fallback, kind="interactive")
        self.sent.append(message)
        log.info("[outbound:%s] interactive %s", to, payload.get("kind"))
        return message

    def send_template(self, to: str, template: str, *, language: str = "ar") -> OutboundMessage:
        message = OutboundMessage(to=to, text=f"[template:{template}]", template=template)
        self.sent.append(message)
        log.info("[outbound:%s] template %s (%s)", to, template, language)
        return message

    def mark_as_read(self, message_id: str) -> bool:
        return True

    def clear(self) -> None:
        self.sent.clear()


_sender: OutboundSender = LogSender()


def register_sender(sender: OutboundSender) -> None:
    global _sender
    _sender = sender


def get_sender() -> OutboundSender:
    return _sender


# --------------------------------------------------------------------------
# Customer-facing message bodies. Egyptian Arabic, WhatsApp length. Product
# names and sizes stay in Latin script -- a customer who asks again will use
# the English name.
# --------------------------------------------------------------------------


def _item_lines(order: Order) -> str:
    """One line per item, always carrying its variant.

    "1x WANAS Hoodie" is not a confirmable order line; "1x WANAS Hoodie —
    Olive, M" is. Pre-rendered into a single string because WhatsApp templates
    have hard limits on the number of variables.
    """
    lines = []
    for item in order.items:
        bits = [b for b in (item.color, item.size, item.length) if b]
        lines.append(f"{item.quantity}× {item.product_name} — {', '.join(bits)}")
    return "\n".join(lines)


def _savings(order: Order) -> str:
    saved = sum((item.unit_original_price - item.unit_price) * item.quantity for item in order.items)
    return f"\nوفرت: {money(saved)} جنيه" if saved > 0 else ""


def customer_reference(order: Order) -> str:
    """The number to say to a customer.

    Shopify's own once the order lives there, because that is the one staff
    can search for in the admin while the customer is on the phone. Orders
    placed before the move fall back to the internal id, which is still the
    only number they have.
    """
    return order.shopify_order_name or order.order_id


def order_confirmation_text(order: Order) -> str:
    return (
        f"تم تأكيد طلبك ✅\n"
        f"رقم الطلب: {customer_reference(order)}\n"
        f"{_item_lines(order)}\n"
        f"الشحن: {money(order.shipping_fee)} جنيه\n"
        f"الإجمالي: {money(order.total)} جنيه (الدفع عند الاستلام)"
        f"{_savings(order)}\n"
        f"هنبعتلك تحديث أول ما الطلب يتجهز."
    )


_STATUS_TEXT = {
    "Packed": "طلبك {order_id} اتجهز وهيتشحن قريب 📦",
    "Shipped": "طلبك {order_id} في الطريق ليك 🚚 — جهّز {total} جنيه للدفع عند الاستلام.",
    "Delivered": "طلبك {order_id} اتسلم ✅ — نتمنى يعجبك!",
    "Cancelled": "طلبك {order_id} اتلغى. لو ده مش قصدك كلمنا وهنساعدك.",
}


def status_change_text(order: Order, status: str | None = None) -> str | None:
    """The push for one status change.

    `status` is passed explicitly by anything that moves an order through more
    than one stage in a single transaction. The messages are only *sent* after
    that transaction commits, and by then `order.status` is whatever the order
    ended on -- so reading it here made "packed" and "shipped" both come out as
    "shipped", which is one message telling the customer something that had
    not happened yet.
    """
    template = _STATUS_TEXT.get(status or order.status)
    if template is None:
        return None
    return template.format(order_id=customer_reference(order), total=money(order.total))


FEEDBACK_REQUEST_TEXT = (
    "عاملين إيه؟ طلبك {order_id} وصلك. تقيّم تجربتك من 1 لـ 5؟ "
    "وأي ملاحظة تحب تضيفها هتفرق معانا."
)


# --------------------------------------------------------------------------
# Events. Each one is "who gets notified, how" straight out of 01.
# --------------------------------------------------------------------------


def order_confirmed(session: Session, order: Order) -> None:
    """Staff alert + the customer's WhatsApp confirmation.

    Sent to the phone collected at checkout, which is always present -- so an
    order placed on any channel still gets a WhatsApp confirmation.

    The alert row is written inside the order's transaction; the message body
    is rendered now but only *sent* once that transaction commits. Telling a
    customer their order is confirmed and then rolling it back is the worst
    failure available here.
    """
    queues.enqueue(
        session,
        kind=QueueKind.ALERT.value,
        reason="order_confirmed",
        summary=f"New order {order.order_id} — {money(order.total)} EGP, {order.governorate}",
        order_id=order.order_id,
        payload={"total": money(order.total), "items": len(order.items), "channel": order.source_channel},
    )
    text = order_confirmation_text(order)
    phone = order.contact_phone
    order_id = order.order_id
    after_commit(session, lambda: _deliver_confirmation(phone, text, order_id))


def _deliver_confirmation(phone: str, text: str, order_id: str) -> None:
    message = _sender.send_text(phone, text, template="order_confirmation")
    if message.delivered:
        return
    # With no email in Phase 1 a failed WhatsApp confirmation means the
    # customer gets nothing at all, so someone has to call them. Its own
    # transaction, because the order it refers to has already committed.
    from backend.db import session_scope

    with session_scope() as session:
        queues.enqueue(
            session,
            kind=QueueKind.ALERT.value,
            reason="confirmation_delivery_failed",
            summary=f"WhatsApp confirmation for {order_id} failed to send to {phone}",
            order_id=order_id,
            payload={"error": message.error or "unknown"},
        )


def order_status_changed(session: Session, order: Order, status: str | None = None) -> None:
    status = status or order.status
    text = status_change_text(order, status)
    if text:
        _sender.send_text(order.contact_phone, text, template=f"status_{status.lower()}")
    if status == "Delivered":
        order_delivered(session, order)


def order_delivered(session: Session, order: Order) -> None:
    """Feedback request, always over WhatsApp regardless of where the order was
    placed -- TikTok cannot be messaged first at all."""
    _sender.send_text(
        order.contact_phone,
        FEEDBACK_REQUEST_TEXT.format(order_id=customer_reference(order)),
        template="feedback_request",
    )


BACK_IN_STOCK_TEXT = "خبر حلو! {product_name} رجع متوفر تاني ✅ حابب تطلبه دلوقتي؟"
ABANDONED_CART_TEXT = "لسه طلبك في السلة 🛒 حابب تكمله؟ لو محتاج مساعدة قولّلي."


def send_proactive(
    session: Session,
    channel: str,
    external_id: str,
    text: str,
    *,
    template: str | None,
    alert_reason: str,
    alert_summary: str,
    alert_payload: dict | None = None,
) -> None:
    """A message the shop starts, not a reply to one just received.

    Free-form text only works inside Meta's 24-hour customer service window
    (`CUSTOMER_SERVICE_WINDOW`), measured from `ChannelIdentity.last_seen_at`
    -- the last time this identity actually messaged in. Outside it, only an
    approved template may reopen the conversation; `template` is that
    template's name, when one has been approved (none are yet -- see
    docs/OPERATIONS.md). With no template configured, or if Meta refuses the
    template send too, there is no other channel to this customer in Phase 1
    (no email, no SMS) -- so the fallback is a staff alert, the same "a
    person has to do it" shape as `_deliver_confirmation`'s failure path.
    """
    if channel != "whatsapp":
        # Nothing else is wired to an outbound sender yet -- see
        # `order_delivered`'s note on TikTok.
        return

    identity = identities.get(session, channel, external_id)
    window_open = bool(
        identity
        and identity.last_seen_at
        and utcnow() - identity.last_seen_at < CUSTOMER_SERVICE_WINDOW
    )

    message = None
    if window_open:
        message = _sender.send_text(external_id, text, template=template)
    elif template:
        message = _sender.send_template(
            external_id, template, language=settings.whatsapp_template_language
        )

    if message is not None and message.delivered:
        return

    queues.enqueue(
        session,
        kind=QueueKind.ALERT.value,
        reason=alert_reason,
        summary=alert_summary,
        channel=channel,
        external_id=external_id,
        payload={
            **(alert_payload or {}),
            "text": text,
            "template": template,
            "window_open": window_open,
            "error": getattr(message, "error", None) or "no approved template configured",
        },
    )


def low_stock_breach(session: Session, variant: Variant) -> None:
    queues.enqueue(
        session,
        kind=QueueKind.ALERT.value,
        reason="low_stock",
        summary=(
            f"Low stock: {variant.product.name} — "
            f"{', '.join(b for b in (variant.color, variant.size, variant.length) if b)} "
            f"({variant.stock_qty} left)"
        ),
        payload={
            "variant_id": variant.variant_id,
            "stock_qty": variant.stock_qty,
            "threshold": variant.low_stock_threshold,
        },
    )


def order_modified(session: Session, order: Order, detail: str) -> None:
    """Visibility, not approval -- the change has already been applied."""
    queues.enqueue(
        session,
        kind=QueueKind.ALERT.value,
        reason="order_modified",
        summary=f"{order.order_id} modified: {detail}",
        order_id=order.order_id,
        payload={"total": money(order.total)},
    )


def order_cancelled(session: Session, order: Order, *, by: str = "customer") -> None:
    queues.enqueue(
        session,
        kind=QueueKind.ALERT.value,
        reason="order_cancelled",
        summary=f"{order.order_id} cancelled by {by} — stock returned",
        order_id=order.order_id,
        payload={"total": money(order.total)},
    )


def item_swap_requested(session: Session, order: Order, payload: dict, summary: str) -> str:
    item = queues.enqueue(
        session,
        kind=QueueKind.ITEM_SWAP.value,
        reason="swap_requested",
        summary=summary,
        order_id=order.order_id,
        channel=payload.get("channel"),
        external_id=payload.get("external_id"),
        payload=payload,
    )
    return item.queue_id
