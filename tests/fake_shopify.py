"""An in-memory stand-in for the Shopify shelf.

The order path now asks Shopify whether a sale may happen. Without something
here, every test that places an order would only prove that a missing token
refuses -- which is true, and useless.

This is not a mock that records calls and asserts on them. It is a working
shelf: it holds quantities, honours `compareQuantity`, and refuses to go
negative, so the tests exercise the real reserve-and-compensate logic rather
than a script of expected calls. Seeded from the same `Variant` rows the tests
already assert against, so "Shopify agrees with wanas.db" is the default and a
test that cares about disagreement creates it explicitly.
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from decimal import Decimal

from backend.services import (
    shopify_admin_customers,
    shopify_admin_orders,
    shopify_admin_products,
    shopify_catalog,
    shopify_inventory,
    shopify_orders,
    shopify_webhooks,
)
from backend.services.shopify_catalog import LiveVariant
from domain.models import Product, Variant


class FakeShopify:
    def __init__(self):
        # variant_id -> {"qty", "price", "compare", "active", "tracked"}
        self.shelf: dict[str, dict] = {}
        #: Set to raise from every read and write, to test the outage path.
        self.down = False
        self._lock = threading.Lock()
        self.orders: dict[str, dict] = {}
        self._order_seq = 0
        self._orders = shopify_orders
        self.reserved: list[tuple[str, int]] = []
        self.released: list[tuple[str, int]] = []
        # product gid -> {"id","title","status","category","description_html","image_url"}
        self.products: dict[str, dict] = {}
        # variant_id (sku) -> product gid
        self.variant_to_product: dict[str, str] = {}
        # variant_id (sku) -> {"size","color","length"}
        self.variant_options: dict[str, dict] = {}
        self._product_seq = 0
        # [{"id","topic","callbackUrl"}, ...]
        self.webhook_subscriptions: list[dict] = []
        self._webhook_seq = 0
        #: Topics that raise WebhookRejected instead of registering, keyed by
        #: Shopify's GraphQL enum name -- for testing the "Shopify refused
        #: this one" path.
        self.rejected_webhook_topics: set[str] = set()

    # -- seeding ---------------------------------------------------------

    def seed_from(self, session):
        """Copy the catalog's current stock and prices onto the shelf, and
        group its variants into one fake Shopify "product" per local
        `Product` row -- so `product_gid_for_variant_id` and `update_product`
        are testable against the seeded catalog, not only against a product
        this fake itself created.

        Run once, while fixtures are being built, so the default state is
        "Shopify and wanas.db agree". A test that wants them to disagree --
        "someone bought the last one on the storefront" -- says so with `set`,
        which is also the only honest way to express it now: changing a
        `Variant` row no longer changes what the order path checks.
        """
        session.expire_all()
        variants = session.query(Variant).all()
        rows = [
            (
                v.variant_id,
                v.stock_qty,
                v.price,
                v.original_price,
                v.on_sale,
                v.product_id,
                v.size,
                v.color,
                v.length,
            )
            for v in variants
        ]
        products = {p.product_id: (p.name, p.category) for p in session.query(Product).all()}
        # `domain.db` opens every SQLite transaction with BEGIN IMMEDIATE, so
        # even this read takes a write lock. Left open, it deadlocks the very
        # next thing the test does.
        session.rollback()

        for variant_id, stock_qty, price, original_price, on_sale, product_id, size, color, length in rows:
            self.shelf.setdefault(
                variant_id,
                {
                    "qty": stock_qty,
                    "price": Decimal(str(price)),
                    "compare": Decimal(str(original_price)) if on_sale else None,
                    "active": True,
                    "tracked": True,
                },
            )
            self.variant_options.setdefault(variant_id, {"size": size, "color": color, "length": length})

            product_gid = f"gid://shopify/Product/seed-{product_id}"
            if product_gid not in self.products:
                name, category = products.get(product_id, (product_id, ""))
                self.products[product_gid] = {
                    "id": product_gid,
                    "title": name,
                    "status": "ACTIVE",
                    "category": category,
                    "description_html": "",
                    "image_url": None,
                }
            self.variant_to_product.setdefault(variant_id, product_gid)
        return self

    def set(self, variant_id, *, qty=None, price=None, compare=None, active=None, tracked=None):
        entry = self.shelf.setdefault(
            variant_id,
            {"qty": 0, "price": Decimal("0"), "compare": None, "active": True, "tracked": True},
        )
        if qty is not None:
            entry["qty"] = qty
        if price is not None:
            entry["price"] = Decimal(str(price))
        if compare is not None:
            entry["compare"] = Decimal(str(compare))
        if active is not None:
            entry["active"] = active
        if tracked is not None:
            entry["tracked"] = tracked
        return self

    def qty(self, variant_id):
        return self.shelf[variant_id]["qty"]

    # -- the read side ---------------------------------------------------

    def _live(self, variant_id) -> LiveVariant | None:
        entry = self.shelf.get(variant_id)
        if entry is None:
            return None
        price = entry["price"]
        compare = entry["compare"]
        return LiveVariant(
            variant_id=variant_id,
            shopify_id=f"gid://shopify/ProductVariant/{variant_id}",
            inventory_item_id=f"gid://shopify/InventoryItem/{variant_id}",
            price=price,
            original_price=compare if compare and compare > price else price,
            stock_qty=entry["qty"] if entry["tracked"] else 999,
            tracked=entry["tracked"],
            product_active=entry["active"],
        )

    def fetch_all(self):
        self._guard()
        return {vid: self._live(vid) for vid in self.shelf}

    def fetch_skus(self, variant_ids):
        self._guard()
        out = {}
        for vid in variant_ids:
            live = self._live(vid)
            if live is not None:
                out[vid] = live
        return out

    def _guard(self):
        if self.down:
            raise shopify_catalog.ShopifyUnavailable("fake shopify is down")

    # -- the write side --------------------------------------------------

    def _adjust(self, changes, sign):
        self._guard()
        # Shopify applies an adjustment atomically. Without this lock the
        # concurrency test passes or fails on thread timing rather than on
        # whether the code handles a lost race -- and it would pass for the
        # wrong reason more often than not.
        with self._lock:
            self._adjust_locked(changes, sign)

    def _adjust_locked(self, changes, sign):
        # Validate every line before touching any, the way an all-or-nothing
        # mutation behaves. A half-applied adjustment would let a test pass
        # against behaviour Shopify does not have.
        planned = []
        for change in changes:
            vid = str(change["inventory_item_id"]).rsplit("/", 1)[-1]
            entry = self.shelf.get(vid)
            if entry is None:
                raise shopify_inventory.StockMoved(f"unknown inventory item {vid}")
            delta = sign * abs(int(change["quantity"]))
            expected = int(change["expected"])
            if entry["qty"] != expected:
                raise shopify_inventory.StockMoved(
                    f"{vid}: expected {expected}, shelf has {entry['qty']}"
                )
            if entry["qty"] + delta < 0:
                raise shopify_inventory.StockMoved(f"{vid}: would go negative")
            planned.append((vid, entry, delta))

        for vid, entry, delta in planned:
            entry["qty"] += delta
            (self.reserved if delta < 0 else self.released).append((vid, abs(delta)))

    # -- orders ----------------------------------------------------------
    #
    # Shopify decrements the shelf as part of creating the order and puts it
    # back when the order is cancelled. The fake does the same, because the
    # double-decrement bug this replaced -- reserving *and* letting the order
    # decrement -- is invisible unless the stand-in models the coupling.

    def create_order(
        self,
        *,
        reference,
        items,
        shipping_fee,
        customer_name="",
        phone="",
        email=None,
        address="",
        governorate="",
        **_ignored,
    ):
        self._guard()
        with self._lock:
            short = []
            for item in items:
                vid = str(item["shopify_variant_id"]).rsplit("/", 1)[-1]
                entry = self.shelf.get(vid)
                if entry is None:
                    short.append(f"{vid}: not on Shopify")
                elif entry["tracked"] and entry["qty"] < int(item["quantity"]):
                    short.append(f"{vid}: insufficient inventory")
            if short:
                raise self._orders.OrderRejected(
                    "; ".join(short), [{"message": m} for m in short]
                )

            self._order_seq += 1
            number = 1000 + self._order_seq
            order = {
                "id": f"gid://shopify/Order/{number}",
                "name": f"#{number}",
                "reference": reference,
                "cancelled": False,
                "fulfilled": False,
                "created_at": datetime.now(UTC).isoformat(),
                "customer_name": customer_name,
                "phone": phone,
                "email": email,
                "address": address,
                "governorate": governorate,
                "shipping_fee": Decimal(str(shipping_fee)),
                "tags": ["chatbot", "whatsapp", "cash-on-delivery"],
                "lines": {},
                #: sku -> {title, unit_price}, so a swap/quantity edit can still
                #: describe the line without a second lookup.
                "line_meta": {},
            }
            for item in items:
                vid = str(item["shopify_variant_id"]).rsplit("/", 1)[-1]
                qty = int(item["quantity"])
                order["lines"][vid] = order["lines"].get(vid, 0) + qty
                order["line_meta"][vid] = {
                    "title": f"variant {vid}",
                    "unit_price": Decimal(str(item.get("unit_price", 0))),
                }
                if self.shelf[vid]["tracked"]:
                    self.shelf[vid]["qty"] -= qty
            self.orders[order["id"]] = order
            return {"id": order["id"], "name": order["name"]}

    def cancel_order(self, shopify_order_id, *, reason="CUSTOMER", restock=True):
        self._guard()
        with self._lock:
            order = self.orders.get(shopify_order_id)
            if order is None:
                raise self._orders.OrderRejected(f"no order {shopify_order_id}")
            if order["cancelled"]:
                raise self._orders.OrderRejected("already cancelled")
            order["cancelled"] = True
            if restock:
                for vid, qty in order["lines"].items():
                    if self.shelf[vid]["tracked"]:
                        self.shelf[vid]["qty"] += qty

    def try_cancel(self, shopify_order_id, *, reason="OTHER"):
        try:
            self.cancel_order(shopify_order_id, reason=reason)
            return True
        except Exception:
            return False

    def set_line_quantity(self, shopify_order_id, sku, quantity, *, note=None):
        self._guard()
        with self._lock:
            order = self.orders.get(shopify_order_id)
            if order is None:
                raise self._orders.OrderRejected(f"no order {shopify_order_id}")
            if sku not in order["lines"]:
                raise self._orders.OrderRejected(f"{sku} is not a line on that order")
            delta = int(quantity) - order["lines"][sku]
            entry = self.shelf[sku]
            if delta > 0 and entry["tracked"] and entry["qty"] < delta:
                raise self._orders.OrderRejected(f"{sku}: insufficient inventory")
            if entry["tracked"]:
                entry["qty"] -= delta
            order["lines"][sku] = int(quantity)

    def swap_line(self, shopify_order_id, from_sku, to_variant_id, quantity, *, note=None):
        self._guard()
        to_sku = str(to_variant_id).rsplit("/", 1)[-1]
        with self._lock:
            order = self.orders.get(shopify_order_id)
            if order is None:
                raise self._orders.OrderRejected(f"no order {shopify_order_id}")
            if from_sku not in order["lines"]:
                raise self._orders.OrderRejected(f"{from_sku} is not a line on that order")
            target = self.shelf.get(to_sku)
            if target is None:
                raise self._orders.OrderRejected(f"{to_sku}: not on Shopify")
            if target["tracked"] and target["qty"] < int(quantity):
                raise self._orders.OrderRejected(f"{to_sku}: insufficient inventory")

            if target["tracked"]:
                target["qty"] -= int(quantity)
            back = order["lines"].pop(from_sku)
            if self.shelf[from_sku]["tracked"]:
                self.shelf[from_sku]["qty"] += back
            order["lines"][to_sku] = order["lines"].get(to_sku, 0) + int(quantity)

    def seed_order(self, *, customer_name="", phone="", email=None, address="", governorate="", items, shipping_fee=0):
        """A order as if placed on the storefront directly -- never through
        `create_order`, so it carries none of the bot's own tags. This is the
        only honest way to get a "website" order into the fake: `create_order`
        *is* the bot's `orderCreate` call, tags and all, so calling it can
        never produce anything the dashboard should label differently."""
        with self._lock:
            self._order_seq += 1
            number = 1000 + self._order_seq
            order = {
                "id": f"gid://shopify/Order/{number}",
                "name": f"#{number}",
                "reference": "website",
                "cancelled": False,
                "fulfilled": False,
                "created_at": datetime.now(UTC).isoformat(),
                "customer_name": customer_name,
                "phone": phone,
                "email": email,
                "address": address,
                "governorate": governorate,
                "shipping_fee": Decimal(str(shipping_fee)),
                "tags": [],
                "lines": {},
                "line_meta": {},
            }
            for item in items:
                vid = str(item["variant_id"])
                qty = int(item["quantity"])
                order["lines"][vid] = order["lines"].get(vid, 0) + qty
                order["line_meta"][vid] = {
                    "title": item.get("title", f"variant {vid}"),
                    "unit_price": Decimal(str(item.get("unit_price", 0))),
                }
                if vid in self.shelf and self.shelf[vid]["tracked"]:
                    self.shelf[vid]["qty"] -= qty
            self.orders[order["id"]] = order
            return order["id"]

    # -- admin product list / detail / create / edit ----------------------
    #
    # Fakes the small network-calling seams `shopify_admin_products.py`
    # itself factors `create_product` / `update_product` into -- not those
    # two functions themselves, which stay real so their local-DB mirroring
    # is always exercised by a test, not reimplemented by this fake.

    def _product_summary_fake(self, p):
        skus = [sku for sku, gid in self.variant_to_product.items() if gid == p["id"]]
        return {
            "id": p["id"],
            "title": p["title"],
            "status": p["status"],
            "category": p["category"],
            "image_url": p.get("image_url"),
            "variant_count": len(skus),
            "any_in_stock": any(self.shelf.get(sku, {}).get("qty", 0) > 0 for sku in skus),
        }

    def list_products(self, *, query=None, cursor=None):
        self._guard()
        return {
            "products": [self._product_summary_fake(p) for p in self.products.values()],
            "has_next_page": False,
            "end_cursor": None,
        }

    def get_product(self, shopify_gid):
        self._guard()
        p = self.products.get(shopify_gid)
        if p is None:
            return None
        out = self._product_summary_fake(p)
        out["description_html"] = p["description_html"]
        skus = [sku for sku, gid in self.variant_to_product.items() if gid == shopify_gid]
        out["variants"] = [
            {
                "id": f"gid://shopify/ProductVariant/{sku}",
                "sku": sku,
                "price": str(self.shelf[sku]["price"]),
                "compare_at_price": str(self.shelf[sku]["compare"]) if self.shelf[sku]["compare"] else None,
                "inventory_quantity": self.shelf[sku]["qty"],
                **self.variant_options.get(sku, {"size": None, "color": None, "length": None}),
            }
            for sku in skus
            if sku in self.shelf
        ]
        return out

    def product_gid_for_variant_id(self, variant_id):
        self._guard()
        return self.variant_to_product.get(variant_id)

    def shopify_create_product(self, *, title, description, category):
        self._guard()
        with self._lock:
            self._product_seq += 1
            gid = f"gid://shopify/Product/{self._product_seq}"
            self.products[gid] = {
                "id": gid,
                "title": title,
                "status": "ACTIVE",
                "category": category,
                "description_html": description or "",
                "image_url": None,
            }
            return gid

    def shopify_set_product_options(self, product_gid, options):
        self._guard()  # nothing to model: implied by shopify_create_variants below

    def shopify_create_variants(self, product_gid, bulk_input):
        self._guard()
        with self._lock:
            created = []
            for entry in bulk_input:
                sku = entry["sku"]
                self.shelf[sku] = {
                    "qty": 0,
                    "price": Decimal(entry["price"]),
                    "compare": Decimal(entry["compareAtPrice"]) if entry.get("compareAtPrice") else None,
                    "active": True,
                    "tracked": True,
                }
                self.variant_to_product[sku] = product_gid
                opts = {ov["optionName"].lower(): ov["name"] for ov in entry.get("optionValues") or []}
                self.variant_options[sku] = {
                    "size": opts.get("size"),
                    "color": opts.get("color"),
                    "length": opts.get("length"),
                }
                created.append({"sku": sku, "inventory_item_id": f"gid://shopify/InventoryItem/{sku}"})
            return created

    def shopify_attach_media(self, product_gid, image_url):
        self._guard()
        if product_gid in self.products:
            self.products[product_gid]["image_url"] = image_url

    def shopify_set_inventory(self, quantities):
        self._guard()
        with self._lock:
            for q in quantities:
                sku = str(q["inventory_item_id"]).rsplit("/", 1)[-1]
                if sku in self.shelf:
                    self.shelf[sku]["qty"] = int(q["quantity"])

    def shopify_update_product_fields(self, product_gid, fields):
        self._guard()
        with self._lock:
            p = self.products.get(product_gid)
            if p is None:
                return
            if "title" in fields:
                p["title"] = fields["title"]
            if "descriptionHtml" in fields:
                p["description_html"] = fields["descriptionHtml"]
            if "productType" in fields:
                p["category"] = fields["productType"]

    def shopify_update_variants(self, product_gid, bulk_input):
        self._guard()
        with self._lock:
            for entry in bulk_input:
                sku = str(entry["id"]).rsplit("/", 1)[-1]
                if sku not in self.shelf:
                    continue
                if "price" in entry:
                    self.shelf[sku]["price"] = Decimal(entry["price"])
                if "compareAtPrice" in entry:
                    self.shelf[sku]["compare"] = Decimal(entry["compareAtPrice"]) if entry["compareAtPrice"] else None
                # `shopify_product_import.py` re-keys a variant that has no
                # SKU wanas.db recognises yet -- rename the shelf entry (and
                # everything else keyed by sku) rather than only tracking
                # price/stock, the way every other caller of this mutation
                # has used it until now.
                new_sku = entry.get("sku")
                if new_sku is not None and new_sku != sku:
                    self.shelf[new_sku] = self.shelf.pop(sku)
                    self.variant_to_product[new_sku] = self.variant_to_product.pop(sku, product_gid)
                    if sku in self.variant_options:
                        self.variant_options[new_sku] = self.variant_options.pop(sku)

    # -- admin customers ---------------------------------------------------
    #
    # No dedicated customer state: every order already carries who placed
    # it, so a "customer" here is just orders grouped by phone (falling back
    # to email, then the order id) -- close enough to what Shopify itself
    # groups by, and enough to test the dashboard's list/detail routes
    # without inventing a second identity model the real Shopify side
    # (`shopify_admin_customers.py`) does not have either.

    def _customer_key(self, order):
        return order.get("phone") or order.get("email") or order["id"]

    def _customers_grouped(self):
        grouped: dict[str, list] = {}
        for order in self.orders.values():
            grouped.setdefault(self._customer_key(order), []).append(order)
        return grouped

    def list_customers(self, *, query=None, cursor=None):
        self._guard()
        customers = []
        for key, group in self._customers_grouped().items():
            first = group[0]
            spent = sum((Decimal(self._order_summary(o)["total"]) for o in group), Decimal("0"))
            customers.append(
                {
                    "id": f"gid://shopify/Customer/{key}",
                    "name": first.get("customer_name"),
                    "email": first.get("email"),
                    "phone": first.get("phone"),
                    "order_count": len(group),
                    "amount_spent": str(spent),
                    "governorate": first.get("governorate"),
                }
            )
        return {"customers": customers, "has_next_page": False, "end_cursor": None}

    def get_customer(self, shopify_gid):
        self._guard()
        key = str(shopify_gid).rsplit("/", 1)[-1]
        group = self._customers_grouped().get(key)
        if not group:
            return None
        first = group[0]
        spent = sum((Decimal(self._order_summary(o)["total"]) for o in group), Decimal("0"))
        return {
            "id": shopify_gid,
            "name": first.get("customer_name"),
            "email": first.get("email"),
            "phone": first.get("phone"),
            "order_count": len(group),
            "amount_spent": str(spent),
            "governorate": first.get("governorate"),
            "address": first.get("address"),
            "orders": [
                {
                    "id": o["id"],
                    "name": o["name"],
                    "created_at": o["created_at"],
                    "financial_status": "PENDING",
                    "fulfillment_status": "FULFILLED" if o["fulfilled"] else "UNFULFILLED",
                    "total": self._order_summary(o)["total"],
                }
                for o in group
            ],
        }

    # -- admin order list / detail / fulfilment ---------------------------
    #
    # A second, read-mostly surface over the same `self.orders` dict
    # `create_order` already builds -- store-wide list/detail/fulfil, as
    # opposed to the bot's own create/cancel/edit above. Kept as plain dict
    # shaping rather than a second GraphQL-shaped fake, since nothing here
    # talks GraphQL either.

    _RANGE_RE = re.compile(r"created_at:(>=|<=)(\S+)")

    def _matches_query(self, order, query):
        if not query:
            return True
        for op, value in self._RANGE_RE.findall(query):
            try:
                bound = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if bound.tzinfo is None:
                # `date_range.as_query()` sends bare dates ("2026-07-20");
                # `created_at` on every order here is UTC-aware, so the bound
                # needs the same awareness or the comparison below raises
                # instead of comparing.
                bound = bound.replace(tzinfo=UTC)
            created = datetime.fromisoformat(order["created_at"])
            if op == ">=" and created < bound:
                return False
            if op == "<=" and created > bound:
                return False
        return True

    def _order_summary(self, order):
        return {
            "id": order["id"],
            "name": order["name"],
            "created_at": order["created_at"],
            "financial_status": "PENDING",
            "fulfillment_status": "FULFILLED" if order["fulfilled"] else "UNFULFILLED",
            "cancelled": order["cancelled"],
            "tags": list(order["tags"]),
            "customer_name": order["customer_name"],
            "customer_phone": order["phone"],
            "governorate": order["governorate"],
            "total": str(
                sum(
                    (order["line_meta"][sku]["unit_price"] * qty for sku, qty in order["lines"].items()),
                    Decimal("0"),
                )
                + order["shipping_fee"]
            ),
            "line_items": [
                {"title": order["line_meta"][sku]["title"], "quantity": qty, "sku": sku}
                for sku, qty in order["lines"].items()
            ],
            "source": "chatbot" if "chatbot" in order["tags"] else "website",
        }

    def list_orders(self, *, query=None, cursor=None):
        self._guard()
        matching = [o for o in self.orders.values() if self._matches_query(o, query)]
        matching.sort(key=lambda o: o["created_at"], reverse=True)
        return {
            "orders": [self._order_summary(o) for o in matching],
            "has_next_page": False,
            "end_cursor": None,
        }

    def get_order(self, shopify_order_id):
        self._guard()
        order = self.orders.get(shopify_order_id)
        if order is None:
            return None
        fo_status = "CLOSED" if order["fulfilled"] else "OPEN"
        return {
            **self._order_summary(order),
            "note": None,
            "customer_email": order["email"],
            "address": order["address"],
            "subtotal": str(
                sum(
                    (order["line_meta"][sku]["unit_price"] * qty for sku, qty in order["lines"].items()),
                    Decimal("0"),
                )
            ),
            "shipping_fee": str(order["shipping_fee"]),
            "line_items": [
                {"id": f"gid://shopify/LineItem/{sku}", "title": order["line_meta"][sku]["title"], "quantity": qty, "sku": sku}
                for sku, qty in order["lines"].items()
            ],
            "fulfillment_orders": [
                {
                    "id": f"gid://shopify/FulfillmentOrder/{order['name']}",
                    "status": fo_status,
                    "line_items": [
                        {
                            "id": f"gid://shopify/FulfillmentOrderLineItem/{sku}",
                            "remaining_quantity": 0 if order["fulfilled"] else qty,
                            "name": order["line_meta"][sku]["title"],
                            "sku": sku,
                        }
                        for sku, qty in order["lines"].items()
                    ],
                }
            ],
        }

    def fulfill(self, shopify_order_id, *, tracking_number=None, tracking_company=None, notify_customer=False):
        self._guard()
        with self._lock:
            order = self.orders.get(shopify_order_id)
            if order is None:
                return {"error": "order_not_found"}
            if order["fulfilled"]:
                return {"error": "already_fulfilled"}
            order["fulfilled"] = True
            order["tracking_number"] = tracking_number
            order["tracking_company"] = tracking_company
            return {"fulfillment_id": f"gid://shopify/Fulfillment/{order['name']}", "status": "SUCCESS"}

    # -- direct inventory writes -----------------------------------------

    def reserve(self, changes, order_ref):
        self._adjust(changes, -1)

    def release(self, changes, order_ref):
        self._adjust(changes, +1)

    def try_release(self, changes, order_ref):
        try:
            self.release(changes, order_ref)
            return True
        except Exception:
            return False

    # -- webhook subscriptions ---------------------------------------------

    def list_subscriptions(self):
        self._guard()
        return list(self.webhook_subscriptions)

    def create_subscription(self, topic, callback_url):
        self._guard()
        if topic in self.rejected_webhook_topics:
            raise shopify_webhooks.WebhookRejected(f"{topic} is not a valid topic for this app")
        with self._lock:
            self._webhook_seq += 1
            self.webhook_subscriptions.append(
                {
                    "id": f"gid://shopify/WebhookSubscription/{self._webhook_seq}",
                    "topic": topic,
                    "callbackUrl": callback_url,
                }
            )

    # -- wiring ----------------------------------------------------------

    def install(self, monkeypatch):
        monkeypatch.setattr(shopify_catalog, "fetch_all", self.fetch_all)
        monkeypatch.setattr(shopify_catalog, "fetch_skus", self.fetch_skus)
        monkeypatch.setattr(shopify_inventory, "reserve", self.reserve)
        monkeypatch.setattr(shopify_inventory, "release", self.release)
        monkeypatch.setattr(shopify_inventory, "try_release", self.try_release)
        monkeypatch.setattr(shopify_orders, "create_order", self.create_order)
        monkeypatch.setattr(shopify_orders, "cancel_order", self.cancel_order)
        monkeypatch.setattr(shopify_orders, "try_cancel", self.try_cancel)
        monkeypatch.setattr(shopify_orders, "set_line_quantity", self.set_line_quantity)
        monkeypatch.setattr(shopify_orders, "swap_line", self.swap_line)
        monkeypatch.setattr(shopify_admin_customers, "list_customers", self.list_customers)
        monkeypatch.setattr(shopify_admin_customers, "get_customer", self.get_customer)
        monkeypatch.setattr(shopify_admin_orders, "list_orders", self.list_orders)
        monkeypatch.setattr(shopify_admin_orders, "get_order", self.get_order)
        monkeypatch.setattr(shopify_admin_orders, "fulfill", self.fulfill)
        monkeypatch.setattr(shopify_admin_products, "list_products", self.list_products)
        monkeypatch.setattr(shopify_admin_products, "get_product", self.get_product)
        monkeypatch.setattr(
            shopify_admin_products, "product_gid_for_variant_id", self.product_gid_for_variant_id
        )
        monkeypatch.setattr(shopify_admin_products, "shopify_create_product", self.shopify_create_product)
        monkeypatch.setattr(
            shopify_admin_products, "shopify_set_product_options", self.shopify_set_product_options
        )
        monkeypatch.setattr(shopify_admin_products, "shopify_create_variants", self.shopify_create_variants)
        monkeypatch.setattr(shopify_admin_products, "shopify_attach_media", self.shopify_attach_media)
        monkeypatch.setattr(shopify_admin_products, "shopify_set_inventory", self.shopify_set_inventory)
        monkeypatch.setattr(
            shopify_admin_products, "shopify_update_product_fields", self.shopify_update_product_fields
        )
        monkeypatch.setattr(shopify_admin_products, "shopify_update_variants", self.shopify_update_variants)
        monkeypatch.setattr(shopify_webhooks, "list_subscriptions", self.list_subscriptions)
        monkeypatch.setattr(shopify_webhooks, "create_subscription", self.create_subscription)
        return self
