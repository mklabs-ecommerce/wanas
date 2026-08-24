"""Backfill wanas.db for Shopify products that were never created through the
dashboard.

`shopify_admin_products.create_product`'s own docstring says why this is
needed: a product added straight from Shopify Admin pushes to Shopify only --
nothing mirrors it into `Product` / `Variant`, and `catalog.get_products`
(the bot's search) reads *only* wanas.db, never the live Shopify product
list. See that module's docstring, and `catalog.py`'s. The result is exactly
what it looks like from WhatsApp: a product staff can see in Shopify Admin
that the bot insists does not exist.

This closes the gap the same direction `create_product` already works in --
Shopify first, wanas.db mirrored after -- except here Shopify already has the
product, so there is nothing to push; only the mirror and the SKU write-back
are new.

Scope, deliberately narrow: only a product with **zero** variant SKUs
recognised by wanas.db is imported. A product already known (any one variant
SKU matches) is left alone even if it gained a colour or size directly in
Shopify Admin since -- reconciling a partial variant addition needs matching
by option values against an existing product, which is a different, fuzzier
problem than "this whole product is invisible". And a product with variants
that already carry *some* SKU, just not one of ours, is reported rather than
silently re-keyed: that SKU was set on purpose by someone, and guessing what
it should say instead is exactly the fragility `shopify_set_skus.py`'s
docstring warns about.

Everything here is additive. An existing local product or variant is never
edited, and nothing is ever deleted, on either side.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Variant
from integrations.shopify import admin_products as admin
from integrations.shopify.admin_products import _mirror_local, _unique_product_id, _variant_id

log = logging.getLogger("wanas.shopify.product_import")


def _read_all_active_products() -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        page = admin.list_products(cursor=cursor)
        out.extend(p for p in page["products"] if (p.get("status") or "").upper() == "ACTIVE")
        if not page.get("has_next_page"):
            break
        cursor = page.get("end_cursor")
        if cursor is None:
            break
    return out


def _prepare_variants(detail: dict) -> list[dict] | None:
    variants = detail.get("variants") or []
    if not variants:
        return None
    prepared = []
    for v in variants:
        price = Decimal(str(v.get("price") or "0"))
        compare = v.get("compare_at_price")
        original_price = Decimal(str(compare)) if compare and Decimal(str(compare)) > price else price
        prepared.append(
            {
                "shopify_variant_id": v["id"],
                "size": v.get("size") or "One Size",
                "color": v.get("color"),
                "length": v.get("length"),
                "price": price,
                "original_price": original_price,
                "stock_qty": int(v.get("inventory_quantity") or 0),
            }
        )
    return prepared


def import_missing_products(session: Session, *, apply: bool = False) -> dict:
    """Every Shopify product whose variants carry no SKU wanas.db recognises.

    Dry run (`apply=False`, the default) only reports what it would do -- the
    same contract every `scripts/shopify_*.py` migration makes. `apply=True`
    creates the local rows and writes wanas.db's own SKU convention back onto
    the Shopify variants, so the very next live read matches them by price
    and stock the same way any other product does.

    Returns `{"checked": n, "imported": [...], "problems": [...]}`.
    """
    known_ids = {row[0] for row in session.execute(select(Variant.variant_id)).all()}

    summaries = _read_all_active_products()
    imported: list[dict] = []
    problems: list[str] = []

    for summary in summaries:
        detail = admin.get_product(summary["id"])
        if detail is None:
            continue

        prepared = _prepare_variants(detail)
        if not prepared:
            continue

        existing_skus = {(v.get("sku") or "").strip() for v in detail.get("variants") or []}
        if existing_skus & known_ids:
            continue  # already linked -- the original seed or a dashboard create

        foreign = sorted(s for s in existing_skus if s)
        if foreign:
            problems.append(
                f"{summary['title']!r} has no wanas.db SKU but already carries "
                f"{foreign} on Shopify -- needs a human look, not a guess"
            )
            continue

        title = summary.get("title") or "Untitled"
        product_id = _unique_product_id(session, title)
        category = summary.get("category") or "Uncategorized"
        image_url = summary.get("image_url")

        if not apply:
            imported.append({"product_id": product_id, "title": title, "variants": len(prepared)})
            continue

        # Shopify first, wanas.db mirrored only once that succeeds -- the same
        # order `create_product` uses, and for the same reason: a local row
        # with no SKU written back to match it would look, on the very next
        # run, exactly like a product nobody has imported yet, and get
        # imported a second time under a new slug. Failing here leaves
        # nothing local to clean up; it is simply retried next run.
        bulk_input = [
            {
                "id": v["shopify_variant_id"],
                "sku": _variant_id(product_id, v["size"], v.get("color"), v.get("length")),
            }
            for v in prepared
        ]
        try:
            admin.shopify_update_variants(summary["id"], bulk_input)
        except Exception as exc:  # ShopifyUnavailable / ShopifyConfigError / ProductRejected
            log.warning("could not write SKUs to Shopify for %r, skipping this run: %s", title, exc)
            problems.append(f"{title!r}: could not write SKUs back to Shopify ({exc}) -- will retry")
            continue

        _mirror_local(
            session,
            product_id=product_id,
            title=title,
            description=detail.get("description_html") or "",
            category=category,
            # Shopify has no department field; a product added outside the
            # dashboard (which does ask) gets the majority default and is one
            # dashboard edit away from being corrected.
            department="unisex",
            style=[],
            collection=None,
            size_chart=None,
            variants=prepared,
            image_url=image_url,
        )
        log.info("imported %r from Shopify as %s (%d variant(s))", title, product_id, len(prepared))
        imported.append({"product_id": product_id, "title": title, "variants": len(prepared)})

    return {"checked": len(summaries), "imported": imported, "problems": problems}
