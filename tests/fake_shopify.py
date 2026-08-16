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

import threading
from decimal import Decimal

from backend.models import Variant
from backend.services import shopify_catalog, shopify_inventory, shopify_orders
from backend.services.shopify_catalog import LiveVariant


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

    # -- seeding ---------------------------------------------------------

    def seed_from(self, session):
        """Copy the catalog's current stock and prices onto the shelf.

        Run once, while fixtures are being built, so the default state is
        "Shopify and wanas.db agree". A test that wants them to disagree --
        "someone bought the last one on the storefront" -- says so with `set`,
        which is also the only honest way to express it now: changing a
        `Variant` row no longer changes what the order path checks.
        """
        session.expire_all()
        variants = session.query(Variant).all()
        rows = [
            (v.variant_id, v.stock_qty, v.price, v.original_price, v.on_sale) for v in variants
        ]
        # `backend.db` opens every SQLite transaction with BEGIN IMMEDIATE, so
        # even this read takes a write lock. Left open, it deadlocks the very
        # next thing the test does.
        session.rollback()

        for variant_id, stock_qty, price, original_price, on_sale in rows:
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

    def create_order(self, *, reference, items, shipping_fee, **_ignored):
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
                "lines": {},
            }
            for item in items:
                vid = str(item["shopify_variant_id"]).rsplit("/", 1)[-1]
                qty = int(item["quantity"])
                order["lines"][vid] = order["lines"].get(vid, 0) + qty
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
        return self
