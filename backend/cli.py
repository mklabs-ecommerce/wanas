"""Operational commands.

    python -m backend.cli init-db
    python -m backend.cli seed
    python -m backend.cli create-staff <username>
    python -m backend.cli set-fee <governorate> <fee>
    python -m backend.cli catalog-report
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select

from backend.db import engine, session_scope
from backend.models import Base, Product, ShippingRate, Variant
from backend.seed.governorates import import_governorates
from backend.seed.products import import_products
from backend.services.auth import create_staff
from backend.services.size_charts import all_charts


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
