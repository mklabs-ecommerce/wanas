"""Prove the chatbot's live read actually works, before a customer tests it.

Runs the real read path against the real store and reports three things:

  1. Can we reach Shopify at all, with the token in .env.
  2. Does every wanas.db variant have a SKU on Shopify -- the mapping the whole
     design rests on. A variant missing here is one the bot will quietly serve
     stale numbers for.
  3. Where Shopify and wanas.db disagree on price or stock. Disagreement is
     expected and is the point; the bot now follows Shopify. This lists it so
     nothing is a surprise.

Writes nothing, anywhere.

    python scripts/shopify_check_live.py
    python scripts/shopify_check_live.py --product wanas-hoodie   # one product
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.integrations.shopify_client import (  # noqa: E402
    ShopifyConfigError,
    ShopifyUnavailable,
    get_client,
)
from backend.services import shopify_catalog  # noqa: E402
from config.settings import settings  # noqa: E402
from domain.db import session_scope  # noqa: E402
from domain.models import Product, Variant  # noqa: E402
from domain.services import catalog  # noqa: E402


def head(text: str) -> None:
    print(f"\n{text}\n" + "-" * len(text))


def check_connection() -> bool:
    head("1. CONNECTION")
    if not settings.shopify_configured:
        print("  ! SHOPIFY_STORE_DOMAIN / SHOPIFY_ADMIN_TOKEN missing from .env")
        print("    The bot will serve wanas.db prices and stock until they are set.")
        return False

    client = get_client()
    print(f"  store   : {client.domain}")
    print(f"  version : {client.version}")

    started = time.monotonic()
    try:
        data = client("{ shop { name currencyCode } }")
    except ShopifyConfigError as exc:
        print(f"  ! {exc}")
        return False
    except ShopifyUnavailable as exc:
        print(f"  ! could not reach Shopify: {exc}")
        return False

    shop = data.get("shop") or {}
    print(f"  name    : {shop.get('name')}  ({shop.get('currencyCode')})")
    print(f"  latency : {(time.monotonic() - started) * 1000:.0f} ms")
    return True


def check_skus(live) -> int:
    head("2. SKU MAPPING")

    with session_scope() as db:
        rows = db.query(Variant.variant_id).all()
    db_ids = {r[0] for r in rows}

    matched = db_ids & set(live)
    missing = sorted(db_ids - set(live))
    extra = sorted(set(live) - db_ids)

    print(f"  wanas.db variants        : {len(db_ids)}")
    print(f"  Shopify variants with SKU: {len(live)}")
    print(f"  matched                  : {len(matched)}")

    if missing:
        print(f"\n  ! {len(missing)} variants have no SKU on Shopify -- the bot will serve")
        print("    wanas.db numbers for these. Run: python scripts/shopify_set_skus.py")
        for vid in missing[:15]:
            print(f"      - {vid}")
        if len(missing) > 15:
            print(f"      ... and {len(missing) - 15} more")

    if extra:
        print(f"\n  ! {len(extra)} SKUs on Shopify have no wanas.db row:")
        for sku in extra[:10]:
            print(f"      - {sku}")

    return len(missing)


def check_values(live, only_product: str | None) -> None:
    head("3. WHERE SHOPIFY AND wanas.db DISAGREE")
    print("  (Shopify wins -- this is what the bot will now say.)\n")

    price_diffs, stock_diffs, inactive = [], [], []

    with session_scope() as db:
        query = db.query(Variant).join(Product)
        if only_product:
            query = query.filter(Product.product_id == only_product)
        for variant in query.all():
            found = live.get(variant.variant_id)
            if found is None:
                continue
            label = f"{variant.product.name} · {variant.color or '-'}/{variant.size}"

            if float(found.price) != float(variant.price):
                price_diffs.append((label, variant.price, found.price))
            if not found.product_active:
                inactive.append(label)
            elif found.stock_qty != variant.stock_qty:
                stock_diffs.append((label, variant.stock_qty, found.stock_qty))

    if inactive:
        print(f"  NOT ACTIVE ON SHOPIFY ({len(inactive)}) -- bot treats these as sold out")
        for label in inactive[:20]:
            print(f"    {label}")
        print()

    if price_diffs:
        print(f"  PRICE ({len(price_diffs)})")
        for label, was, now in price_diffs[:30]:
            print(f"    {label}: db {was} -> shopify {now}")
        if len(price_diffs) > 30:
            print(f"    ... and {len(price_diffs) - 30} more")
        print()

    if stock_diffs:
        print(f"  STOCK ({len(stock_diffs)})")
        for label, was, now in stock_diffs[:30]:
            print(f"    {label}: db {was} -> shopify {now}")
        if len(stock_diffs) > 30:
            print(f"    ... and {len(stock_diffs) - 30} more")
        print()

    if not (price_diffs or stock_diffs or inactive):
        print("  None -- the two sides agree exactly.")


def sample_reply(only_product: str | None) -> None:
    """The end-to-end proof: what a tool would actually hand the model."""
    head("4. WHAT THE BOT WOULD SEE")

    with session_scope() as db:
        product_id = only_product
        if not product_id:
            first = db.query(Product).order_by(Product.name).first()
            if first is None:
                print("  no products in wanas.db")
                return
            product_id = first.product_id

        payload = catalog.get_variants(db, product_id)
        if payload is None:
            print(f"  no product {product_id!r}")
            return

        print(f"  {payload['name']}  ({product_id})")
        print(f"  in stock: {len(payload['in_stock'])} of {len(payload['variants'])} variants\n")
        for v in payload["variants"][:12]:
            sale = f"  was {v['original_price']}" if v["on_sale"] else ""
            bits = " / ".join(x for x in (v["color"], v["length"], v["size"]) if x)
            print(f"    {bits:<28} {v['price']:>7}  {v['status']:<9}{sale}")
        if len(payload["variants"]) > 12:
            print(f"    ... and {len(payload['variants']) - 12} more")


def check_write(variant_id: str) -> bool:
    """Take one unit off the shelf and put it straight back.

    The read path can be perfect while the write path is broken -- a token
    without `write_inventory`, or no permission on the location -- and the
    first time anyone would find out is a customer confirming an order. This
    proves it now, and leaves the shelf exactly as it found it.
    """
    head("5. WRITE PERMISSION")
    from backend.services import shopify_inventory

    live = shopify_catalog.fetch_skus([variant_id]).get(variant_id)
    if live is None:
        print(f"  ! {variant_id} is not on Shopify -- cannot test the write path")
        return False
    if not live.tracked:
        print(f"  {variant_id} is not inventory-tracked; nothing to reserve")
        return True

    before = live.stock_qty
    print(f"  variant : {variant_id}")
    print(f"  location: {shopify_inventory.location_id()}")
    print(f"  before  : {before}")

    change = [
        {
            "inventory_item_id": live.inventory_item_id,
            "quantity": 1,
            "expected": before,
        }
    ]

    try:
        shopify_inventory.reserve(change, "check-live")
    except shopify_inventory.StockMoved as exc:
        print(f"  ! Shopify refused the reservation: {exc}")
        return False
    except ShopifyConfigError as exc:
        # ShopifyConfigError is the one that really is about credentials --
        # the client raises it for 401 and 403 specifically.
        print(f"  ! Could not reserve: {exc}")
        print("    The token is missing write_inventory, or has no access to this location.")
        return False
    except ShopifyUnavailable as exc:
        print(f"  ! Could not reserve: {exc}")
        if "not defined on" in str(exc) or "INVALID_VARIABLE" in str(exc):
            # Guessing "permissions" at every failure sends you to the admin to
            # re-check scopes that were fine all along.
            print("    That is a schema mismatch, not a permissions problem.")
            print(f"    Check the mutation against API version {settings.shopify_api_version}.")
        return False

    after_reserve = shopify_catalog.fetch_skus([variant_id])[variant_id].stock_qty
    print(f"  reserved: {after_reserve}")

    returned = shopify_inventory.try_release(
        [
            {
                "inventory_item_id": live.inventory_item_id,
                "quantity": 1,
                "expected": after_reserve,
            }
        ],
        "check-live",
    )
    after = shopify_catalog.fetch_skus([variant_id])[variant_id].stock_qty
    print(f"  after   : {after}")

    if not returned or after != before:
        print(f"  ! The unit was NOT returned. Set {variant_id} back to {before} by hand.")
        return False

    print("  Reserve and release both work, and the shelf is back where it started.")
    return True


def check_order(variant_id: str) -> bool:
    """Create a real order for one unit, then cancel it.

    The write test above proves the inventory API works; it says nothing about
    `orderCreate`, which needs a different scope, a valid shipping line, and an
    address Shopify will accept. Those fail in ways that only appear on a live
    store, and the first person to discover them should not be a customer.

    It leaves a cancelled order in the admin. That is deliberate -- a visible
    trace beats a silent one -- and the stock comes back with it.
    """
    head("6. ORDER PATH")
    from backend.services import shopify_orders

    live = shopify_catalog.fetch_skus([variant_id]).get(variant_id)
    if live is None:
        print(f"  ! {variant_id} is not on Shopify")
        return False
    if live.tracked and live.stock_qty < 1:
        print(f"  ! {variant_id} has no stock; pick one that does")
        return False

    before = live.stock_qty
    print(f"  variant : {variant_id}")
    print(f"  before  : {before}")

    try:
        created = shopify_orders.create_order(
            reference="check-live",
            items=[
                {
                    "shopify_variant_id": live.shopify_id,
                    "quantity": 1,
                    "unit_price": live.price,
                }
            ],
            customer_name="Wanas Test",
            # Deliberately the local form a customer would type. Shopify
            # rejects it outright unless create_order translates it, and
            # a check that sends a pre-translated number proves nothing.
            phone="01000000000",
            email=None,
            address="Verification order — cancel me",
            governorate="Cairo",
            shipping_fee=60,
            note="Automated check from scripts/shopify_check_live.py. Safe to delete.",
        )
    except shopify_orders.OrderRejected as exc:
        print(f"  ! Shopify refused to create the order: {exc}")
        return False
    except ShopifyConfigError as exc:
        print(f"  ! Could not create the order: {exc}")
        print("    The token is missing write_orders.")
        return False
    except ShopifyUnavailable as exc:
        print(f"  ! Could not create the order: {exc}")
        if "not defined on" in str(exc) or "INVALID_VARIABLE" in str(exc):
            print("    That is a schema mismatch, not a permissions problem.")
            print(f"    Check the mutation against API version {settings.shopify_api_version}.")
        return False

    print(f"  created : {created['name']}")
    after_create = shopify_catalog.fetch_skus([variant_id])[variant_id].stock_qty
    print(f"  stock   : {after_create}  (Shopify decremented it with the order)")

    cancelled = shopify_orders.try_cancel(created["id"], reason="OTHER")
    if not cancelled:
        print(f"  ! {created['name']} could not be cancelled. Cancel it in the admin.")
        return False

    # orderCancel is a job, not an edit. Reading the shelf immediately after it
    # returns shows the pre-cancel number every time -- which looks exactly
    # like a restock that never happened.
    after = before - 1
    for _ in range(10):
        time.sleep(1.0)
        after = shopify_catalog.fetch_skus([variant_id])[variant_id].stock_qty
        if after == before:
            break
    print(f"  after   : {after}")

    if after_create != before - 1:
        print("  ! Creating the order did not take the unit off the shelf.")
        return False
    if after != before:
        print("  ! The unit did not come back after 10s.")
        print(f"    Check {created['name']} in the admin; set {variant_id} to {before} by hand.")
        return False

    print(f"  {created['name']} is cancelled and the shelf is back where it started.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", help="limit sections 3 and 4 to one product_id")
    parser.add_argument(
        "--write-test",
        metavar="VARIANT_ID",
        help="reserve one unit of this variant and release it again, to prove the write path",
    )
    parser.add_argument(
        "--order-test",
        metavar="VARIANT_ID",
        help="create a real one-item order for this variant and cancel it again",
    )
    args = parser.parse_args()

    if not check_connection():
        print("\nStopping: nothing below can be checked without a connection.")
        sys.exit(1)

    try:
        live = shopify_catalog.fetch_all()
    except (ShopifyUnavailable, ShopifyConfigError) as exc:
        print(f"\n! Could not read the catalog: {exc}")
        sys.exit(1)

    missing = check_skus(live)

    # Hand the snapshot to the service layer so sections 3 and 4 read exactly
    # what the bot reads, without a second round trip.
    shopify_catalog.prime(live)

    check_values(live, args.product)
    sample_reply(args.product)

    write_ok = True
    if args.write_test:
        write_ok = check_write(args.write_test)

    order_ok = True
    if args.order_test:
        order_ok = check_order(args.order_test)

    head("VERDICT")
    if missing:
        print(f"  {missing} variants still need SKUs. Run scripts/shopify_set_skus.py first.")
        sys.exit(1)
    if not write_ok:
        print("  Reads work, but the write path does not. The bot will refuse every order.")
        sys.exit(1)
    if not order_ok:
        print("  The order path did not complete cleanly -- read section 6 before launching.")
        sys.exit(1)
    print("  Live read is working. Every variant maps to Shopify.")
    if args.write_test:
        print("  Live write is working too.")
    if args.order_test:
        print("  Orders can be created and cancelled on Shopify.")


if __name__ == "__main__":
    main()
