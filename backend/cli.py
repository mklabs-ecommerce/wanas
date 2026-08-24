"""Operational commands.

    python -m backend.cli init-db
    python -m backend.cli seed
    python -m backend.cli create-staff <username>
    python -m backend.cli set-fee <governorate> <fee>
    python -m backend.cli catalog-report
    python -m backend.cli inspect-conversation <external_id> [--channel whatsapp]
    python -m backend.cli release-conversation <external_id> [--channel whatsapp]
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select

from domain.db import engine, session_scope
from domain.models import (
    Base,
    ChannelIdentity,
    Product,
    QueueKind,
    QueueStatus,
    SessionRow,
    ShippingRate,
    StaffQueueItem,
    Variant,
)
from domain.seed.governorates import import_governorates
from domain.seed.products import import_products
from domain.services import (
    identities,
    queues,
)
from domain.services.auth import create_staff
from domain.services.size_charts import all_charts


def cmd_init_db(_args) -> int:
    Base.metadata.create_all(engine)
    print(f"schema created on {engine.url.render_as_string(hide_password=True)}")
    return 0


def cmd_seed(_args) -> int:
    Base.metadata.create_all(engine)
    with session_scope() as session:
        product_stats = import_products(session)
        gov_stats = import_governorates(session)
    print(
        f"catalog: {product_stats['products']} products, "
        f"{product_stats['variants']} variants, {product_stats['in_stock']} in stock"
    )
    print(f"shipping: {gov_stats['governorates']} governorates seeded (fees blank until the shop sets them)")
    print(f"size charts: {len(all_charts())} charts loaded from data/size_charts.json")
    return 0


def _read_password(prompt: str) -> str:
    """Hidden entry at a terminal, plain stdin when piped.

    getpass reads from the console device, not stdin, so it blocks forever
    under a pipe or a CI runner instead of failing.
    """
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            raise EOFError("no password on stdin")
        return line.rstrip("\n")
    return getpass.getpass(prompt)


def cmd_create_staff(args) -> int:
    try:
        password = _read_password("password: ")
        confirm = _read_password("confirm: ")
    except EOFError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if password != confirm:
        print("passwords do not match", file=sys.stderr)
        return 1
    try:
        with session_scope() as session:
            staff = create_staff(session, args.username, password)
            print(f"created staff #{staff.staff_id} {staff.username}")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_set_fee(args) -> int:
    with session_scope() as session:
        rate = session.get(ShippingRate, args.governorate)
        if rate is None:
            print(f"unknown governorate {args.governorate!r}", file=sys.stderr)
            return 1
        rate.fee = args.fee
        print(f"{rate.governorate}: {args.fee} EGP")
    return 0


def cmd_catalog_report(_args) -> int:
    with session_scope() as session:
        products = session.scalar(select(func.count()).select_from(Product))
        variants = session.scalar(select(func.count()).select_from(Variant))
        in_stock = session.scalar(select(func.count()).select_from(Variant).where(Variant.stock_qty > 0))
        print(f"{products} products / {variants} variants / {in_stock} in stock")
        print()
        print(f"{'category':<24} {'products':>8} {'variants':>9}")
        rows = session.execute(
            select(
                Product.category,
                func.count(func.distinct(Product.product_id)),
                func.count(Variant.variant_id),
            )
            .join(Variant, Variant.product_id == Product.product_id)
            .group_by(Product.category)
            .order_by(func.count(Variant.variant_id).desc())
        ).all()
        for category, n_products, n_variants in rows:
            print(f"{category:<24} {n_products:>8} {n_variants:>9}")
        print()
        print("shipping rates without a fee: ", end="")
        missing = session.scalar(
            select(func.count()).select_from(ShippingRate).where(ShippingRate.fee.is_(None))
        )
        total = session.scalar(select(func.count()).select_from(ShippingRate))
        print(f"{missing} of {total}")
    return 0


def cmd_inspect_conversation(args) -> int:
    """Everything an operator needs to decide whether a conversation is stuck."""
    with session_scope() as session:
        identity = session.get(ChannelIdentity, (args.channel, args.external_id))
        if identity is None:
            print(f"no conversation for {args.channel}/{args.external_id}", file=sys.stderr)
            return 1
        print(f"conversation {identity.channel}/{identity.external_id}")
        print(f"paused_until_staff_reply: {identity.paused_until_staff_reply}")
        print(f"client_id: {identity.client_id}")
        print(f"last_seen_at: {identity.last_seen_at.isoformat() if identity.last_seen_at else None}")
        row = session.get(SessionRow, (identity.channel, identity.external_id))
        try:
            history_len = len(row.history) if row is not None else 0
        except Exception:
            history_len = "UNREADABLE (not valid JSON)"
        print(f"history messages: {history_len}")
        items = session.scalars(
            select(StaffQueueItem)
            .where(
                StaffQueueItem.channel == identity.channel,
                StaffQueueItem.external_id == identity.external_id,
                StaffQueueItem.status == QueueStatus.OPEN.value,
            )
            .order_by(StaffQueueItem.created_at.desc())
        ).all()
        print(f"open staff_queue items: {len(items)}")
        for item in items:
            print(
                f"  {item.queue_id}  {item.kind:<8} reason={item.reason or '-':<16} "
                f"created {item.created_at.isoformat() if item.created_at else '-'}"
            )
    return 0


def cmd_release_conversation(args) -> int:
    """The manual escape hatch for a latched pause.

    Clears `paused_until_staff_reply` and resolves the open handoff items, so
    the bot answers the next message again. Never runs on its own -- only a
    person decides a conversation leaves a person.
    """
    with session_scope() as session:
        identity = session.get(ChannelIdentity, (args.channel, args.external_id))
        if identity is None:
            print(f"no conversation for {args.channel}/{args.external_id}", file=sys.stderr)
            return 1
        was_paused = identity.paused_until_staff_reply
        identities.unpause(session, identity.channel, identity.external_id)
        items = session.scalars(
            select(StaffQueueItem).where(
                StaffQueueItem.channel == identity.channel,
                StaffQueueItem.external_id == identity.external_id,
                StaffQueueItem.kind == QueueKind.HANDOFF.value,
                StaffQueueItem.status == QueueStatus.OPEN.value,
            )
        ).all()
        for item in items:
            # No staff account to attribute this to; the column stays null.
            queues.resolve(session, item.queue_id, None)
        print(f"conversation {identity.channel}/{identity.external_id}")
        print(f"paused_until_staff_reply: {was_paused} -> False")
        print(f"resolved open handoff items: {len(items)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backend.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the schema").set_defaults(func=cmd_init_db)
    sub.add_parser("seed", help="import catalog + governorates").set_defaults(func=cmd_seed)

    p_staff = sub.add_parser(
        "create-staff", help="create a staff account (used to attribute resolved queue items)"
    )
    p_staff.add_argument("username")
    p_staff.set_defaults(func=cmd_create_staff)

    p_fee = sub.add_parser("set-fee", help="set a governorate shipping fee")
    p_fee.add_argument("governorate")
    p_fee.add_argument("fee", type=float)
    p_fee.set_defaults(func=cmd_set_fee)

    sub.add_parser("catalog-report", help="print catalog counts").set_defaults(func=cmd_catalog_report)

    p_inspect = sub.add_parser(
        "inspect-conversation",
        help="show the pause flag, last seen, history size and open queue items for one conversation",
    )
    p_inspect.add_argument("external_id")
    p_inspect.add_argument("--channel", default="whatsapp")
    p_inspect.set_defaults(func=cmd_inspect_conversation)

    p_release = sub.add_parser(
        "release-conversation",
        help="clear paused_until_staff_reply and resolve open handoff items (manual escape hatch)",
    )
    p_release.add_argument("external_id")
    p_release.add_argument("--channel", default="whatsapp")
    p_release.set_defaults(func=cmd_release_conversation)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
