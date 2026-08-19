"""The Phase 1 schema.

Four headline databases (07-10) plus the supporting tables in 16. Note that
16 defines *eight* supporting tables while AGENTS.md's inline list names seven
-- it omits `staff_queue`, which every review queue, alert and `request_human`
call in the rest of both documents depends on. Built here as the eight.

Nothing in this file uses a SQLite-specific type or default: PostgreSQL is the
target and SQLite is only a local convenience (AGENTS.md, Tech stack).
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# Money is stored as an exact decimal, never a float: a cash-on-delivery total
# that is off by a rounding error is an argument at the customer's door.
Money = Numeric(10, 2)


# --------------------------------------------------------------------------
# Enumerations kept as plain strings in the database (portable, and readable
# in a psql session) with the allowed values named here.
# --------------------------------------------------------------------------


class OrderStatus(str, enum.Enum):
    # `Pending payment` is deliberately absent: cash on delivery only in
    # Phase 1, so there is no gateway, no payment link and no timeout job
    # (01-backend-platform.md, Payment service).
    CONFIRMED = "Confirmed"
    PACKED = "Packed"
    SHIPPED = "Shipped"
    DELIVERED = "Delivered"
    CANCELLED = "Cancelled"


#: Statuses a customer-driven quantity change or cancellation may still touch.
#: One place owns this rule; `modifiable` in the tool contracts is computed
#: from it rather than derived by the model from a status string.
MODIFIABLE_STATUSES = (OrderStatus.CONFIRMED.value, OrderStatus.PACKED.value)

STATUS_SEQUENCE = (
    OrderStatus.CONFIRMED.value,
    OrderStatus.PACKED.value,
    OrderStatus.SHIPPED.value,
    OrderStatus.DELIVERED.value,
)


class Channel(str, enum.Enum):
    WEBSITE = "website"
    WHATSAPP = "whatsapp"
    FACEBOOK_DM = "facebook_dm"
    INSTAGRAM_DM = "instagram_dm"
    TIKTOK_DM = "tiktok_dm"


class QueueKind(str, enum.Enum):
    ITEM_SWAP = "item_swap"
    HANDOFF = "handoff"
    ALERT = "alert"


class QueueStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    REJECTED = "rejected"


#: `voice_received` is its own reason rather than `out_of_scope`: a voice
#: note the transcriber could not read is a message the shop still wants,
#: and staff working the queue need to see it is waiting on a listen.
HANDOFF_REASONS = (
    "unclear",
    "complaint",
    "customer_asked",
    "image_received",
    "voice_received",
    "out_of_scope",
)
ALERT_REASONS = ("order_confirmed", "low_stock", "order_modified", "order_cancelled", "swap_requested",
                 "confirmation_delivery_failed")


# --------------------------------------------------------------------------
# 07 - Client database
# --------------------------------------------------------------------------


class Client(Base):
    __tablename__ = "clients"

    client_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Nullable, and null for every Phase 1 order: the WhatsApp flow never asks
    # for an email, which is what keeps an email provider out of this phase.
    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    address: Mapped[str] = mapped_column(Text, nullable=False, default="")
    governorate: Mapped[str | None] = mapped_column(String(60), nullable=True)
    has_account: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    orders: Mapped[list[Order]] = relationship(back_populates="client")

    @property
    def public_id(self) -> str:
        return f"c_{self.client_id}"


# --------------------------------------------------------------------------
# 08 - Product database
# --------------------------------------------------------------------------


class Product(Base):
    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    department: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    style: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    collection: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    size_chart: Mapped[str | None] = mapped_column(String(60), nullable=True)

    # Display summaries derived from the variants. Never the basis of an
    # availability decision -- per-axis lists claim combinations that do not
    # exist (08-product-database.md).
    sizes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    colors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    lengths: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # The lowest variant price and the highest pre-discount price. Neither is
    # quotable on its own; the catalog service recomputes min/max of the live
    # variant prices for every reply.
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    original_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    on_sale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    images: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    color_images: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_products: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    variants: Mapped[list[Variant]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="Variant.variant_id"
    )


class Variant(Base):
    __tablename__ = "variants"

    variant_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"), nullable=False, index=True)
    size: Mapped[str] = mapped_column(String(20), nullable=False)
    # Present on all 208 variants today; nullable only for a future colourless
    # product. Colour and length stay separate columns rather than a generic
    # "option 2" because they are different questions to the customer.
    color: Mapped[str | None] = mapped_column(String(60), nullable=True)
    length: Mapped[str | None] = mapped_column(String(20), nullable=True)

    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    original_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    on_sale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # The only field that is decremented, and the only one that can race.
    stock_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=2)

    product: Mapped[Product] = relationship(back_populates="variants")

    @property
    def status(self) -> str:
        """Computed at read time, never stored, so it cannot go stale relative
        to the number it describes (08-product-database.md)."""
        if self.stock_qty <= 0:
            return "sold_out"
        if self.stock_qty <= self.low_stock_threshold:
            return "low_stock"
        return "in_stock"


# --------------------------------------------------------------------------
# 09 - Order database
# --------------------------------------------------------------------------


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.client_id"), nullable=False, index=True)
    source_channel: Mapped[str] = mapped_column(String(20), nullable=False)

    # Copied onto the order, not read back through the client: a customer who
    # moves must not silently re-point orders already delivered to the old
    # address, and a one-time shipping address has nowhere else to live.
    shipping_address: Mapped[str] = mapped_column(Text, nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(40), nullable=False)
    governorate: Mapped[str] = mapped_column(String(60), nullable=False)

    # Four separate numbers, never collapsed: shipping is not revenue and a
    # discount has to be visible as its own number to be reportable.
    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    discount_code_used: Mapped[str | None] = mapped_column(String(60), nullable=True)
    discount_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=0)
    shipping_fee: Mapped[Decimal] = mapped_column(Money, nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, nullable=False)

    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False, default="cash_on_delivery")
    payment_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    packed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    modification_log: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # The same order as Shopify holds it. `order_id` above stays the primary
    # key and the internal reference -- the queues and the modification log
    # all point at it -- while these two are how the same sale is found in
    # the admin.
    #
    # Nullable on purpose. Orders placed before the move have neither, and a
    # NOT NULL here would mean either inventing an id for history or refusing
    # to load it.
    shopify_order_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    #: What the customer is told -- Shopify's own "#1004". Stored rather than
    #: derived, because it is what was said out loud at the time.
    shopify_order_name: Mapped[str | None] = mapped_column(String(40), nullable=True)

    client: Mapped[Client] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )

    @property
    def modifiable(self) -> bool:
        return self.status in MODIFIABLE_STATUSES


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), nullable=False, index=True)
    variant_id: Mapped[str] = mapped_column(ForeignKey("variants.variant_id"), nullable=False)

    # Snapshots. The variant is the reference; these are what the packing slip
    # says. A catalog rename must not rewrite order history.
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    size: Mapped[str] = mapped_column(String(20), nullable=False)
    color: Mapped[str | None] = mapped_column(String(60), nullable=True)
    length: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    unit_original_price: Mapped[Decimal] = mapped_column(Money, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


# --------------------------------------------------------------------------
# 10 - Order feedback
# --------------------------------------------------------------------------


class OrderFeedback(Base):
    __tablename__ = "order_feedback"

    feedback_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # One row per order -- enforced here rather than checked in application
    # code, so `already_rated` cannot be raced past.
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), nullable=False, unique=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.client_id"), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# 16 - Supporting tables
# --------------------------------------------------------------------------


class ChannelIdentity(Base):
    __tablename__ = "channel_identities"

    channel: Mapped[str] = mapped_column(String(20), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    # Stays null until the customer confirms the link. A conversation, a
    # session and a cart can all belong to nobody right up until the first
    # order -- code that assumes otherwise falls over on the first new customer.
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.client_id"), nullable=True)
    paused_until_staff_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # An unconfirmed phone/email match waiting for an "is this you?" answer.
    # Surfaced by get_my_profile; only link_client acts on it.
    pending_link: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SessionRow(Base):
    __tablename__ = "sessions"

    channel: Mapped[str] = mapped_column(String(20), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    # The neutral message list from 02-chatbot.md. In the database rather than
    # process memory so the server can restart, and so more than one instance
    # can run behind a load balancer.
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CartItem(Base):
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # The only thing that can be in a cart. There is no "product + chosen
    # options" path anywhere in the system.
    variant_id: Mapped[str] = mapped_column(ForeignKey("variants.variant_id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # One line per variant per identity; quantity carries the rest.
        UniqueConstraint("channel", "external_id", "variant_id", name="uq_cart_identity_variant"),
    )


class ShippingRate(Base):
    __tablename__ = "shipping_rates"

    # English and stable, because it is a primary key; the Arabic label is for
    # display. Every spelling variant becoming its own row is exactly what this
    # avoids.
    governorate: Mapped[str] = mapped_column(String(60), primary_key=True)
    label_ar: Mapped[str] = mapped_column(String(80), nullable=False)
    # Null until the shop prices it. An order for an unpriced governorate is
    # refused rather than shipped free.
    fee: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("staff.staff_id"), nullable=True)


class StaffQueueItem(Base):
    __tablename__ = "staff_queue"

    # The three review queues are one table with a `kind`, not three: they
    # share every field that matters and staff work them from one list, which
    # is also what makes a single unread count possible.
    queue_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=QueueStatus.OPEN.value, index=True
    )
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.order_id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("staff.staff_id"), nullable=True)


class Staff(Base):
    __tablename__ = "staff"

    staff_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Deactivate rather than delete, so `resolved_by` on old queue items keeps resolving.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuntimeSetting(Base):
    """A staff-toggled override for a handful of named feature flags.

    `backend/config.py`'s `Settings` stays env-only and frozen -- that
    boundary does not move. This is a separate, deliberately small overlay:
    the same "Shopify's number over wanas.db's" shape (`catalog._overlay`),
    just for `voice_notes_enabled` / `image_understanding_enabled` /
    `interactive_messages_enabled` instead of price and stock. Absent means
    "use the env default"; a row here is a staff decision made from the
    dashboard, without a redeploy.
    """

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("staff.staff_id"), nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    # Platforms retry delivery when they do not get a fast enough response.
    # Without this row a retry creates a duplicate order or double-decrements
    # stock.
    platform_message_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Counter(Base):
    """Infrastructure, not a domain table.

    Order IDs are `WNS-<n>` from 1001 and queue IDs are `SWAP-<n>` / `HO-<n>` /
    `ALERT-<n>`. Deriving the next one from `max(order_id)` races under
    concurrent checkout; a row updated in place inside the same transaction
    does not, on either database.
    """

    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class TestPhoneNumber(Base):
    """Numbers staff use to test the bot by chatting with it for real.

    The bot treats these exactly like any other customer -- this table
    changes nothing about how a conversation is handled. It exists only so
    `backend/services/dashboard_stats.py` can leave a test purchase out of
    revenue/order-count/best-sellers, the same way a cancelled order already
    is: testing should not make the shop's own numbers look busier than they
    are.
    """

    __tablename__ = "test_phone_numbers"

    phone: Mapped[str] = mapped_column(String(40), primary_key=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    added_by: Mapped[int | None] = mapped_column(ForeignKey("staff.staff_id"), nullable=True)


class WhatsAppMedia(Base):
    """Cache of Meta media IDs for the 12 size-chart images.

    There are only twelve and they change rarely; re-uploading a
    several-hundred-KB PNG on every sizing question is a slow reply for no
    reason (04-whatsapp-channel.md).
    """

    __tablename__ = "whatsapp_media"

    path: Mapped[str] = mapped_column(String(255), primary_key=True)
    media_id: Mapped[str] = mapped_column(String(120), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


__all__ = [
    "Base",
    "Client",
    "Product",
    "Variant",
    "Order",
    "OrderItem",
    "OrderFeedback",
    "ChannelIdentity",
    "SessionRow",
    "CartItem",
    "ShippingRate",
    "StaffQueueItem",
    "Staff",
    "RuntimeSetting",
    "TestPhoneNumber",
    "WebhookEvent",
    "Counter",
    "WhatsAppMedia",
    "OrderStatus",
    "Channel",
    "QueueKind",
    "QueueStatus",
    "MODIFIABLE_STATUSES",
    "STATUS_SEQUENCE",
    "HANDOFF_REASONS",
    "ALERT_REASONS",
    "utcnow",
]
