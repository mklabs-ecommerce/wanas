"""Notification service.

The only thing that turns internal events into actual messages, so the Order
and Inventory modules stay about business rules rather than message formatting.

Two destinations: the customer over WhatsApp, and staff via the alert queue
(`StaffQueueItem`). There is no email provider, because the WhatsApp flow
never collects an email.

Outbound delivery is a port, not an import. `LogSender` is the default, so
every proactive message is produced, formatted and observable before any Meta
credential exists; registering the real client at startup is the only change.
This is also what keeps the dependency direction one-way: domain/ knows the
interface, and never imports assistant/.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Protocol

from sqlalchemy.orm import Session

from common.events import after_close, after_commit
from common.money import money
from common.timeutil import as_aware
from config.settings import settings
from domain.models import Order, QueueKind, Variant, utcnow
from domain.services import (
    identities,
    queues,
)

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
    #: The ids the platform gave the messages it accepted, when it gives any.
    #: Plural because one logical reply can leave as several sends -- Instagram
    #: chunks long text, and a customer may quote any chunk. The channel
    #: adapter stamps them onto the transcript so a later "reply to this" on a
    #: message the shop sent can be resolved -- see `assistant/quoting.py`.
    #: Empty for a sender that has no such notion (the log sender, the
    #: harness) and for anything that was not delivered.
    message_ids: list[str] = field(default_factory=list)

    @property
    def message_id(self) -> str | None:
        return self.message_ids[0] if self.message_ids else None


class OutboundSender(Protocol):
    def send_text(self, to: str, text: str, *, template: str | None = None) -> OutboundMessage: ...

    def send_image(self, to: str, image_path: str, *, caption: str = "") -> OutboundMessage: ...

    def send_interactive(self, to: str, payload: dict, *, fallback: str = "") -> OutboundMessage: ...

    def send_template(self, to: str, template: str, *, language: str = "ar") -> OutboundMessage: ...

    def mark_as_read(self, message_id: str) -> bool: ...


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

    def send_private_reply(self, comment_id: str, text: str) -> OutboundMessage:
        """Instagram's private reply to a comment -- a no-op here.

        On `LogSender` only, and deliberately off the `OutboundSender`
        protocol: it is an Instagram-only capability WhatsApp has no answer
        to. Recorded like any other send so the tests can assert on comment
        flows with no network.
        """
        message = OutboundMessage(to=f"comment:{comment_id}", text=text)
        self.sent.append(message)
        log.info("[outbound:comment:%s] %s", comment_id, text.replace("\n", " | "))
        return message

    def clear(self) -> None:
        self.sent.clear()


_senders: dict[str, OutboundSender] = {}
_default: OutboundSender = LogSender()


def register_sender(sender: OutboundSender, *, channel: str = "whatsapp") -> None:
    """Register the client that actually delivers on one channel.

    `channel` is keyword-with-a-default so every existing call site and test
    keeps working unchanged; new channels must pass it explicitly.
    """
    _senders[channel] = sender


def get_sender(channel: str | None = None) -> OutboundSender:
    """The sender for one channel, or the LogSender when none is registered.

    A channel with no registered sender falls back to the log and **never**
    to another channel's client: sending an Instagram reply over WhatsApp is
    a message delivered to the wrong person, which is strictly worse than a
    message not sent. `channel=None` means "the default channel" and is kept
    only for the pre-Instagram call sites; passing it in new code is a bug.
    """
    if channel is None:
        return _senders.get("whatsapp", _default)
    return _senders.get(channel, _default)


# --------------------------------------------------------------------------
# The transcript
# --------------------------------------------------------------------------
#
# Everything below sends a message the shop started rather than a reply the
# agent produced -- an order confirmation, a status push, a feedback request,
# a back-in-stock notice, an abandoned-cart nudge. All of them went straight
# to the sender and nowhere else, so none of them appeared in `sessions`, and
# the dashboard showed a conversation the customer's own phone did not agree
# with: a bot that had said things nobody on staff could see it say.
#
# A port, for the same reason `OutboundSender` is one: the transcript lives in
# `assistant/session.py`, one layer up, and domain/ must never import the
# assistant layer. The assistant registers its own writer at startup, exactly
# like `domain/services/conversation_reset.py`'s history clearer.
#
# Every call here passes the transaction that decided the message, so the line
# and the row that records the decision commit together. That is also the only
# thing that works: several of these messages are sent from an after-commit
# hook, and SQLite holds the write lock on the committing connection until the
# hook returns -- a second connection opened in there waits for a lock nothing
# is going to release.

#: (channel, external_id, text, db=Session|None, delivered=bool) -> None.
#: Given a session it writes through it; given none it opens its own.
#: `delivered=False` marks the line as one the customer never received --
#: called a second time for the same text, it updates that line rather than
#: writing it twice. See `assistant/runtime.py::record_outbound`.
TranscriptRecorder = Callable[..., None]

_transcript_recorder: TranscriptRecorder | None = None


def register_transcript_recorder(recorder: TranscriptRecorder) -> None:
    """Called once at startup by the assistant layer. Until it is, proactive
    messages are still sent -- they simply are not written to the transcript,
    which is what tests with no assistant wiring expect."""
    global _transcript_recorder
    _transcript_recorder = recorder


def _record(
    channel: str,
    external_id: str,
    text: str,
    *,
    db: Session | None = None,
    delivered: bool = True,
    message_ids: list[str] | None = None,
) -> None:
    """`delivered=False` writes the line as one that did not reach the phone.

    The dashboard must never show a message as sent when the shop knows, or
    cannot tell, that Meta refused it -- a status push that fell outside the
    24-hour window with no approved template used to be written here exactly
    like one that arrived, so a staff member reading the thread saw the
    customer being kept informed while the customer's phone stayed silent.

    `message_ids` annotates a line already written with the ids the platform
    gave it, which only exist once the send has happened -- see
    `_stamp_ids`. Same port, because it is the same fact about the same
    message: what the shop sent, and what became of it.
    """
    if _transcript_recorder is None or not (text or "").strip():
        return
    try:
        _transcript_recorder(
            channel, external_id, text, db=db, delivered=delivered, message_ids=message_ids
        )
    except Exception:
        # The customer already has the message. Failing to write it down is
        # bad, but turning that into an exception in a Shopify webhook or the
        # scheduler loop would be worse.
        log.exception("could not record an outbound message to %s/%s", channel, external_id)


def _stamp_ids(channel: str, external_id: str, text: str, message: OutboundMessage) -> None:
    """Write down what the platform called this message, once it has said.

    The link between a message in the transcript and a later "the customer
    read it" callback, which names the message by platform id and nothing
    else. An agent reply gets this from its channel adapter
    (`_remember_sent_ids`); a proactive push has no adapter in the loop, so it
    is done here.

    Its own transaction, because every caller is inside an after-commit hook
    or has already finished its own -- and on SQLite a write from inside such
    a hook has to wait for the connection that is holding the lock. Queued
    through `after_close` by the callers that have a unit of work to queue on.
    """
    if not message.delivered or not message.message_ids:
        return
    from domain.db import session_scope

    try:
        with session_scope() as session:
            _record(
                channel, external_id, text, db=session, message_ids=list(message.message_ids)
            )
    except Exception:
        # A missing id costs this message its read receipt and nothing else.
        log.exception("could not record the platform ids for a message to %s", external_id)


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


def window_open(session: Session, channel: str, external_id: str) -> bool:
    """Can a free-form message still reach this identity?

    True while the customer's own last message is less than
    `CUSTOMER_SERVICE_WINDOW` old. Read from `ChannelIdentity.last_seen_at`,
    which is the only record of when they last wrote in.

    Must be called while a transaction is open -- so, for anything sent from
    an after-commit hook, *before* the commit, and the answer carried forward
    (see `PushPlan`). Asking afterwards means a second read on a session that
    has already committed, from inside the hook that is holding the write
    lock.
    """
    identity = identities.get(session, channel, external_id)
    last_seen = as_aware(identity.last_seen_at) if identity else None
    return bool(last_seen and utcnow() - last_seen < CUSTOMER_SERVICE_WINDOW)


def order_confirmed(session: Session, order: Order) -> None:
    """Staff alert + the customer's confirmation.

    Sent on the channel the order was placed on, to the identity that placed
    it -- an Instagram DM, not a WhatsApp thread that may not exist. Orders
    with no recorded identity (placed before `source_external_id` existed)
    fall back to the phone over WhatsApp.

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
    channel, recipient = customer_destination(order)
    order_id = order.order_id
    unit = session
    # The transcript line goes in here, beside the alert row and for the same
    # reason: an order that rolls back must not leave the dashboard showing a
    # confirmation for it. Only the *send* waits for the commit.
    _record(channel, recipient, text, db=session)
    after_commit(
        session,
        lambda: _deliver_confirmation(recipient, text, order_id, channel=channel, unit=unit),
    )


def customer_destination(order: Order) -> tuple[str, str]:
    """Where to reach this customer about this order.

    The identity that placed it, when the order knows -- that is the only
    thread guaranteed to exist on a second channel. Otherwise the phone
    collected at checkout over WhatsApp, which is where every pre-Instagram
    order lived anyway.
    """
    if order.source_channel and order.source_external_id:
        return order.source_channel, order.source_external_id
    return "whatsapp", order.contact_phone


def _deliver_confirmation(
    to: str,
    text: str,
    order_id: str,
    *,
    channel: str = "whatsapp",
    unit: Session | None = None,
) -> None:
    """The confirmation send, from the order's after-commit hook.

    No window check here, and that is deliberate rather than an omission: a
    confirmation is sent seconds after the customer's own "yes, confirm", so
    the 24-hour window is open by construction. `WHATSAPP_TEMPLATE_ORDER_
    CONFIRMATION` exists for the case where it somehow is not -- an order
    placed by staff on a customer's behalf, a retry hours later -- and is
    tried before giving up on it.
    """
    sender = get_sender(channel)
    message = sender.send_text(to, text, template="order_confirmation")
    if not message.delivered and settings.whatsapp_template_order_confirmation:
        log.warning(
            "order %s: the free-form confirmation to %s was refused (%s); trying the "
            "approved template",
            order_id,
            to,
            message.error or "unknown",
        )
        message = sender.send_template(
            to,
            settings.whatsapp_template_order_confirmation,
            language=settings.whatsapp_template_language,
        )
    if message.delivered:
        # What Meta called it, so a later "seen" callback can find this line.
        if unit is not None:
            after_close(unit, lambda: _stamp_ids(channel, to, text, message))
        else:
            _stamp_ids(channel, to, text, message)
        return
    if unit is not None:
        # Writing from inside this hook deadlocks on SQLite (see
        # `common/events.py::after_close`); the follow-up runs the moment the
        # order's own unit of work lets go of the connection.
        after_close(unit, lambda: _confirmation_failed(channel, to, text, order_id, message.error))
        return
    _confirmation_failed(channel, to, text, order_id, message.error)


def _confirmation_failed(
    channel: str, to: str, text: str, order_id: str, error: str | None
) -> None:
    # With no email in Phase 1 a failed confirmation means the
    # customer gets nothing at all, so someone has to call them. Its own
    # transaction, because the order it refers to has already committed.
    from domain.db import session_scope

    with session_scope() as session:
        # ...and the transcript line written back in `order_confirmed` says
        # the customer was told. Correct it, or the dashboard shows a
        # confirmation that only ever existed on this server.
        _record(channel, to, text, db=session, delivered=False)
        queues.enqueue(
            session,
            kind=QueueKind.ALERT.value,
            reason="confirmation_delivery_failed",
            summary=f"Order confirmation for {order_id} failed to send to {to} on {channel}",
            order_id=order_id,
            payload={"error": error or "unknown", "channel": channel},
        )


@dataclass
class PushPlan:
    """One status change's messages, and whether each can actually arrive.

    The window question (`window_open`) needs a live transaction, and the
    sending happens from an after-commit hook where there is none to spare --
    so the answer is decided while the status change is still being written
    and carried across the commit in here, rather than re-read from a session
    that has already committed.

    `messages` is a list because a delivery says two things: the status push
    and, right behind it, the rating request.
    """

    channel: str
    recipient: str
    order_id: str
    status: str
    window_open: bool
    #: (text, approved template name or None, label) in the order they are
    #: sent. The label is what the free-form send is tagged with -- the
    #: harness and the tests read it, and "status_delivered" and
    #: "feedback_request" are two different messages that happen to leave
    #: together.
    messages: list[tuple[str, str | None, str]] = field(default_factory=list)

    def deliverable(self, template: str | None) -> bool:
        """Free-form text inside the window, an approved template outside it,
        and nothing at all otherwise."""
        return self.window_open or bool(template)


def status_push_plan(session: Session, order: Order, status: str | None = None) -> PushPlan:
    """What this status change has to say, and whether it can get there.

    The template names are read here rather than at send time so a plan is a
    complete description of the delivery on its own.
    """
    channel, recipient = customer_destination(order)
    status = status or order.status
    plan = PushPlan(
        channel=channel,
        recipient=recipient,
        order_id=order.order_id,
        status=status,
        window_open=window_open(session, channel, recipient),
    )
    text = status_change_text(order, status)
    if text:
        plan.messages.append(
            (text, settings.whatsapp_template_order_update or None, f"status_{status.lower()}")
        )
    if status == "Delivered":
        plan.messages.append(
            (
                FEEDBACK_REQUEST_TEXT.format(order_id=customer_reference(order)),
                settings.whatsapp_template_feedback_request or None,
                "feedback_request",
            )
        )
    return plan


def record_status_push(session: Session, order: Order, status: str | None = None) -> PushPlan:
    """The transcript half of the status push, written in the transaction.

    Split from the send, and not for tidiness. `deliver_status_push` runs from
    an after-commit hook, where `session` has already committed and can emit
    no more SQL -- and where a *second* connection cannot help either, because
    the committing one holds SQLite's write lock until the hook returns. So
    the line the dashboard reads is written here, while the status change
    itself is still being written, and rolls back with it if it does.

    What is new is *what* that line says. It used to be written as though the
    message had arrived, whatever Meta was going to do with it. Outside the
    24-hour customer service window Meta refuses free-form text outright, so a
    parcel that shipped a day after the customer last wrote produced a
    transcript full of updates the customer never saw, with nothing anywhere
    saying so -- the shop found out only when a customer asked where their
    order was. The window is now decided here: an undeliverable push is
    recorded as undelivered and raises a staff alert in the same transaction,
    and `deliver_status_push` does not attempt the send at all.
    """
    plan = status_push_plan(session, order, status)
    for text, template, _label in plan.messages:
        deliverable = plan.deliverable(template)
        _record(plan.channel, plan.recipient, text, db=session, delivered=deliverable)
        if not deliverable:
            queues.enqueue(
                session,
                kind=QueueKind.ALERT.value,
                reason="status_push_undelivered",
                summary=(
                    f"{customer_reference(order)} is now {plan.status}, but the update "
                    f"cannot reach {plan.recipient} on {plan.channel}: their last message "
                    "is over 24h old and no approved template is configured. Call them."
                ),
                order_id=order.order_id,
                channel=plan.channel,
                external_id=plan.recipient,
                payload={"text": text, "status": plan.status, "window_open": False},
            )
            log.warning(
                "order %s: the %s update to %s/%s cannot be sent (outside the 24h window, "
                "no approved template); a staff alert was raised instead",
                order.order_id,
                plan.status,
                plan.channel,
                plan.recipient,
            )
    return plan


def deliver_status_push(plan: PushPlan, *, unit: Session | None = None) -> None:
    """The outbound half. Sends only -- see `record_status_push`.

    Inside the window the text goes as itself. Outside it only an approved
    template can reopen the conversation, so that is what is sent, while the
    transcript keeps the sentence the customer reads rather than
    "[template:...]" -- the approved copy *is* that message. Anything the
    platform refuses anyway becomes a staff alert and a transcript line marked
    undelivered: the shop is never left believing a tracking update landed
    when it did not.
    """
    def follow_up(text: str, template: str | None, error: str | None) -> None:
        """The correction, once this unit of work has let go of the database.

        `_push_failed` writes, and this runs from an after-commit hook where
        writing through a second connection deadlocks on SQLite -- so it is
        queued for after the session closes. A caller with no `session_scope`
        around it has nothing to queue on and does the work inline.
        """
        if unit is not None:
            after_close(unit, lambda: _push_failed(plan, text, template, error))
        else:
            _push_failed(plan, text, template, error)

    sender = get_sender(plan.channel)
    for text, template, label in plan.messages:
        if not plan.deliverable(template):
            # Already recorded as undelivered and alerted, in the transaction.
            continue
        if plan.window_open:
            message = sender.send_text(plan.recipient, text, template=label)
        else:
            message = sender.send_template(
                plan.recipient, template, language=settings.whatsapp_template_language
            )
        if not message.delivered:
            follow_up(text, template, message.error)
        elif unit is not None:
            # Bound now, not in the closure's variable: `message` and `text`
            # are the loop's, and a plan with two messages would otherwise
            # stamp the second one's ids twice.
            after_close(unit, partial(_stamp_ids, plan.channel, plan.recipient, text, message))
        else:
            _stamp_ids(plan.channel, plan.recipient, text, message)


def _push_failed(plan: PushPlan, text: str, template: str | None, error: str | None) -> None:
    """A send the platform refused, learned about after the commit.

    Its own transaction, like `_deliver_confirmation`'s failure path: the
    status change it refers to has already committed. Both halves matter --
    the alert so a person follows up, and the transcript correction so the
    dashboard stops showing a message the customer never received.
    """
    from domain.db import session_scope

    log.error(
        "order %s: the %s update to %s/%s was refused (%s)",
        plan.order_id,
        plan.status,
        plan.channel,
        plan.recipient,
        error or "unknown",
    )
    with session_scope() as session:
        _record(plan.channel, plan.recipient, text, db=session, delivered=False)
        queues.enqueue(
            session,
            kind=QueueKind.ALERT.value,
            reason="status_push_undelivered",
            summary=(
                f"The {plan.status} update for {plan.order_id} was refused by "
                f"{plan.channel} for {plan.recipient}. Call them."
            ),
            order_id=plan.order_id,
            channel=plan.channel,
            external_id=plan.recipient,
            payload={
                "text": text,
                "status": plan.status,
                "template": template,
                "window_open": plan.window_open,
                "error": error or "unknown",
            },
        )


def order_status_changed(session: Session, order: Order, status: str | None = None) -> None:
    """Record and send in one call, for a caller with no after-commit split to
    make. `orders.advance_status` uses the two halves directly, because there
    the send has to wait for the status change to commit."""
    deliver_status_push(record_status_push(session, order, status), unit=session)


def order_delivered(session: Session, order: Order) -> None:
    """The feedback request on its own, on the same channel the order and its
    updates went out on.

    A normal Delivered transition already carries it (`status_push_plan`);
    this is the path for anything that asks for it by itself, and it obeys the
    same window rule -- a rating request is the message most likely of all to
    fall outside it, since it follows a delivery that may be days after the
    customer last wrote.
    """
    channel, recipient = customer_destination(order)
    plan = PushPlan(
        channel=channel,
        recipient=recipient,
        order_id=order.order_id,
        status="Delivered",
        window_open=window_open(session, channel, recipient),
        messages=[
            (
                FEEDBACK_REQUEST_TEXT.format(order_id=customer_reference(order)),
                settings.whatsapp_template_feedback_request or None,
                "feedback_request",
            )
        ],
    )
    deliver_status_push(plan, unit=session)


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
    template's name, when one has been approved (see docs/OPERATIONS.md for
    the list and for the fact that approving one is a manual step in Meta
    Business Manager). With no template configured, or if Meta refuses the
    template send too, there is no other channel to this customer in Phase 1
    (no email, no SMS) -- so the fallback is a staff alert, the same "a
    person has to do it" shape as `_deliver_confirmation`'s failure path,
    plus a transcript line marked undelivered so the thread itself shows
    what the customer was meant to receive and did not.
    """
    if channel not in _senders:
        # Nothing else is wired to an outbound sender yet -- see
        # `order_delivered`'s note on TikTok. A channel is opt-in: no sender
        # registered means nothing is sent, rather than a send through some
        # other channel's client.
        return

    identity = identities.get(session, channel, external_id)
    last_seen = as_aware(identity.last_seen_at) if identity else None
    window_open = bool(last_seen and utcnow() - last_seen < CUSTOMER_SERVICE_WINDOW)

    message = None
    sender = get_sender(channel)
    if window_open:
        message = sender.send_text(external_id, text, template=template)
    elif template:
        message = sender.send_template(
            external_id, template, language=settings.whatsapp_template_language
        )

    if message is not None and message.delivered:
        # `text` even on the template branch: the template renders to this
        # same sentence on Meta's side, and the transcript has to say what the
        # customer read, not "[template:back_in_stock]".
        _record(channel, external_id, text, db=session)
        # No after-commit hook in the way here -- this runs inside the
        # caller's live transaction, so the ids go on straight away.
        if message.message_ids:
            _record(
                channel, external_id, text, db=session, message_ids=list(message.message_ids)
            )
        return

    # It did not go out. The line is still written, marked undelivered: the
    # dashboard showing nothing at all is how a nudge that never reached
    # anybody looks exactly like one that was never due, and the staff member
    # picking up the alert below needs to see what the customer was *meant*
    # to receive in the thread itself.
    _record(channel, external_id, text, db=session, delivered=False)
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
