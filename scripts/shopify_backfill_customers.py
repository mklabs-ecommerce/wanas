"""Attach a customer to the orders that were placed without one.

The bot only started putting a customer on its orders when
`integrations/shopify/orders.py::_customer` shipped. Every sale before that
reached the admin with a shipping address and no customer, which the Orders
list renders as **No customer** -- and which leaves `numberOfOrders` empty, so
nothing downstream can tell a returning buyer from a first-time one.

The name was never lost. It is on the shipping address, along with the phone.
This walks the orders that have no customer, finds or creates the person from
those two fields, and links them with `orderCustomerSet`.

    python scripts/shopify_backfill_customers.py            # dry run
    python scripts/shopify_backfill_customers.py --apply    # perform the writes

Dry run by default, like every other script in here, and the dry run is the
supervision: it prints one line per order saying which customer it would link
and whether that customer already exists. Read it before applying.

Idempotent. An order that already has a customer is skipped, so re-running
after a partial failure only touches what is left. Two orders from the same
person converge on one customer record -- the second finds what the first
created.

Scope: this walks every order in the shop, not only the bot's. A website order
without a customer is the same repair. `--tag chatbot` narrows it to the bot's
own if you want to start there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations.shopify import admin_customers  # noqa: E402
from integrations.shopify.client import (  # noqa: E402
    ShopifyConfigError,
    ShopifyUnavailable,
    get_admin_client,
)
from integrations.shopify.orders import _split_name, normalise_phone  # noqa: E402

#: Everything needed to decide the repair, and nothing else. Deliberately not
#: `admin_orders.ORDERS_QUERY`: that one is shaped for the dashboard table and
#: does not say whether the *address* carries a first/last name, which is what
#: a customer record is built from.
ORDERS_QUERY = """
query($cursor: String, $query: String) {
  orders(first: 50, after: $cursor, query: $query, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      createdAt
      email
      customer { id }
      shippingAddress { name firstName lastName phone }
    }
  }
}
"""

#: Same ceiling as everywhere else that pages: 20 pages x 50 = 1,000 orders.
MAX_PAGES = 20


def iter_orders(query: str | None, max_pages: int = MAX_PAGES):
    """Every order matching `query`, newest first. Yields raw nodes."""
    client = get_admin_client()
    cursor = None
    for _ in range(max_pages):
        data = client(ORDERS_QUERY, {"cursor": cursor, "query": query})
        block = data.get("orders") or {}
        yield from block.get("nodes") or []
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return
        cursor = page.get("endCursor")
    print(f"  ! stopped at the {max_pages}-page cap; re-run to continue", file=sys.stderr)


def names_from(address: dict) -> tuple[str, str]:
    """First and last name off the shipping address.

    Shopify usually returns `firstName`/`lastName` already split, because the
    order was created with them split. When it has only the combined `name`,
    `_split_name` does it the same way the bot does -- imported rather than
    reimplemented, so the backfilled records match the ones created live.
    """
    first = (address.get("firstName") or "").strip()
    last = (address.get("lastName") or "").strip()
    if first or last:
        return first, last
    return _split_name(address.get("name") or "")


def plan_for(node: dict) -> dict | None:
    """What to do about one order, or None if there is nothing to do.

    Two reasons to skip, and they are different: the order already has a
    customer (done), or it has no phone and no email, in which case there is
    nothing to find or create a customer *by*. A name alone is not an
    identity -- linking two different people who happen to share one would be
    worse than leaving the order as it is.
    """
    if node.get("customer"):
        return None

    address = node.get("shippingAddress") or {}
    first, last = names_from(address)
    phone = normalise_phone(address.get("phone"))
    email = (node.get("email") or "").strip().lower() or None

    if not phone and not email:
        return {"order": node, "skip": "no phone or email to identify them by"}

    return {
        "order": node,
        "first": first,
        "last": last,
        "phone": phone,
        "email": email,
    }


def run(*, apply: bool, tag: str | None, max_pages: int) -> int:
    query = f"tag:{tag}" if tag else None

    scanned = linked = created = skipped = failed = 0
    #: Identifiers a *dry run* has already accounted for. Two orders from the
    #: same person share one customer record -- a dry run writes nothing, so
    #: without this it would report one new customer per order and promise the
    #: operator far more records than it is about to make.
    planned: set[str] = set()

    #: Customers created during *this* run, by identifier.
    #:
    #: Shopify's customer search is an index, and the index lags the write: a
    #: customer created a second ago is not findable yet, so the next order
    #: from that same person searched, found nothing, and tried to create a
    #: duplicate -- which Shopify correctly refused with "Phone has already
    #: been taken". Six of nineteen orders failed that way on the first real
    #: run. What this run created, this run remembers.
    created_here: dict[str, str] = {}

    for node in iter_orders(query, max_pages):
        scanned += 1
        plan = plan_for(node)
        if plan is None:
            continue
        label = f"{node.get('name') or node['id']}  {(node.get('createdAt') or '')[:10]}"

        if "skip" in plan:
            skipped += 1
            print(f"  - {label}  skipped: {plan['skip']}")
            continue

        who = " ".join(part for part in (plan["first"], plan["last"]) if part) or "(no name)"
        identifier = plan["phone"] or plan["email"]

        try:
            existing = (
                None
                if identifier in created_here
                else admin_customers.find_customer(phone=plan["phone"], email=plan["email"])
            )
        except (ShopifyUnavailable, ShopifyConfigError) as exc:
            failed += 1
            print(f"  ! {label}  could not search: {exc}", file=sys.stderr)
            continue

        if existing:
            verb = "link to existing"
            customer_id = existing["id"]
        elif identifier in created_here:
            verb = "link to the one created a moment ago"
            customer_id = created_here[identifier]
        else:
            verb = "create and link"
            customer_id = None

        if not apply:
            first_time = customer_id is None and identifier not in planned
            planned.add(identifier)
            if customer_id is None and not first_time:
                verb = "create and link"
                note = " (same person as an order above)"
            else:
                note = ""
            print(f"  · {label}  would {verb}: {who} <{identifier}>{note}")
            linked += 1
            if first_time:
                created += 1
            continue

        try:
            if customer_id is None:
                customer = admin_customers.create_customer(
                    first_name=plan["first"],
                    last_name=plan["last"],
                    phone=plan["phone"],
                    email=plan["email"],
                )
                customer_id = customer["id"]
                created_here[identifier] = customer_id
                created += 1
            admin_customers.set_order_customer(node["id"], customer_id)
        except (
            admin_customers.CustomerWriteRefused,
            ShopifyUnavailable,
            ShopifyConfigError,
        ) as exc:
            failed += 1
            print(f"  ! {label}  {exc}", file=sys.stderr)
            continue

        linked += 1
        print(f"  ✓ {label}  {verb}: {who} <{identifier}>")

    print()
    print(f"scanned {scanned} orders")
    print(f"{'linked' if apply else 'would link'} {linked}"
          f" ({created} new customer{'' if created == 1 else 's'})")
    if skipped:
        print(f"skipped {skipped} with nothing to identify the buyer by")
    if failed:
        print(f"{failed} failed -- see above")
    if not apply and linked:
        print("\ndry run: nothing was written. Re-run with --apply.")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the writes")
    parser.add_argument("--tag", help="only orders carrying this tag, e.g. chatbot")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    args = parser.parse_args()

    print("Backfilling customers onto orders that have none")
    print(f"  mode: {'APPLY -- writing to Shopify' if args.apply else 'dry run'}")
    print(f"  scope: {('tag:' + args.tag) if args.tag else 'every order'}")
    print()
    try:
        return run(apply=args.apply, tag=args.tag, max_pages=args.max_pages)
    except ShopifyConfigError as exc:
        print(f"Shopify is not configured: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
