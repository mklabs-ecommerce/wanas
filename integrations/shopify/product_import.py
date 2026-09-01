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

from domain.models import Product, Variant
from integrations.shopify import admin_products as admin
from integrations.shopify.admin_products import _mirror_local, _unique_product_id, _variant_id

log = logging.getLogger("wanas.shopify.product_import")


def _adoptable_product_id(detail: dict, prepared: list[dict]) -> str | None:
    """The `product_id` a set of Shopify SKUs was written under, if this
    codebase is what wrote them.

    A SKU that is not in `Variant.variant_id` is normally left alone and
    reported: somebody set it on purpose and guessing what it should say
    instead is the fragility `shopify_set_skus.py` warns about. But there is
    one family of unknown SKU that is not a guess at all -- our own. A
    dashboard create pushes to Shopify *first* and mirrors wanas.db after, so
    a failure in between leaves a Shopify product wearing SKUs in exactly the
    `_variant_id` shape with no local rows behind them. That product was
    invisible to the bot permanently: the reconcile refused it every boot,
    for a reason that did not apply.

    Recognising is not guessing. Every variant's SKU has to reproduce itself
    exactly from `_variant_id(candidate, size, colour, length)`, and all of
    them have to agree on the same candidate. Anything else -- a shop's own
    SKU scheme, a hand-typed code, a mix -- fails the check and is reported
    the way it always was.
    """
    variants = detail.get("variants") or []
    if not variants or len(variants) != len(prepared):
        return None

    candidates = set()
    for raw, ready in zip(variants, prepared, strict=True):
        sku = (raw.get("sku") or "").strip()
        if not sku:
            return None
        # `_variant_id("", ...)` is the suffix the convention would append:
        # "-s-black". What is left in front of it is the only product_id that
        # could have produced this SKU.
        suffix = _variant_id("", ready["size"], ready.get("color"), ready.get("length"))
        if not suffix or not sku.endswith(suffix):
            return None
        candidate = sku[: -len(suffix)]
        rebuilt = _variant_id(candidate, ready["size"], ready.get("color"), ready.get("length"))
        if not candidate or rebuilt != sku:
            return None
        candidates.add(candidate)

    return candidates.pop() if len(candidates) == 1 else None


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


def _import_one(
    session: Session,
    summary: dict,
    detail: dict,
    known_ids: set[str],
    *,
    apply: bool,
) -> tuple[dict | None, str | None]:
    """One product. Returns `(imported_entry, problem)`, at most one of them set.

    Split out of `import_missing_products` so the webhook path can apply the
    identical rules to a single product without a full catalogue read: what
    counts as a placeholder, as already-known, as ours-but-lost and as
    somebody else's SKU has to be decided in one place, or a product would be
    imported by one door and refused by the other.
    """
    # Shopify's own untouched placeholder is not a product. Mirroring one
    # writes a phantom "One Size" row at 0.00 that the bot offers and can
    # never sell, and it outlives the Shopify product it came from -- three of
    # those had to be deleted from production by hand. A half-made product now
    # cleans itself up (`_or_unmake_it` in `admin_products.create_product`);
    # this is the second lock on the same door, for a shell made any other way.
    if admin.is_placeholder_only(detail):
        log.info("skipping %r: still only Shopify's placeholder variant", summary.get("title"))
        return None, None

    prepared = _prepare_variants(detail)
    if not prepared:
        return None, None

    existing_skus = {(v.get("sku") or "").strip() for v in detail.get("variants") or []}
    if existing_skus & known_ids:
        return None, None  # already linked -- the original seed or a dashboard create

    title = summary.get("title") or "Untitled"
    category = summary.get("category") or "Uncategorized"
    image_url = summary.get("image_url")

    foreign = sorted(s for s in existing_skus if s)
    # SKUs we wrote and then lost the local rows for -- a dashboard create
    # that pushed to Shopify and failed before mirroring. Adopting those needs
    # no Shopify write at all: the SKUs on the variants are already the ones
    # `_mirror_local` is about to derive.
    adopted = _adoptable_product_id(detail, prepared) if foreign else None
    if adopted is not None and session.get(Product, adopted) is not None:
        # The id belongs to a different product, so these SKUs cannot be what
        # they look like. Back to reporting rather than colliding.
        adopted = None
    if foreign and adopted is None:
        return None, (
            f"{title!r} has no wanas.db SKU but already carries {foreign} on "
            "Shopify -- needs a human look, not a guess"
        )

    product_id = adopted or _unique_product_id(session, title)
    entry = {
        "product_id": product_id,
        "title": title,
        "variants": len(prepared),
        "adopted": adopted is not None,
    }
    if not apply:
        return entry, None

    if adopted is None:
        # Shopify first, wanas.db mirrored only once that succeeds -- the same
        # order `create_product` uses, and for the same reason: a local row
        # with no SKU written back to match it would look, on the very next
        # run, exactly like a product nobody has imported yet, and get
        # imported a second time under a new slug. Failing here leaves nothing
        # local to clean up; it is simply retried next run.
        bulk_input = [
            {
                "id": v["shopify_variant_id"],
                # Under `inventoryItem`, not at the top level:
                # `ProductVariantsBulkInput` has no `sku` field of its own.
                "inventoryItem": {
                    "sku": _variant_id(product_id, v["size"], v.get("color"), v.get("length")),
                },
            }
            for v in prepared
        ]
        try:
            admin.shopify_update_variants(summary["id"], bulk_input)
        except Exception as exc:  # ShopifyUnavailable / ShopifyConfigError / ProductRejected
            log.warning("could not write SKUs to Shopify for %r, skipping this run: %s", title, exc)
            return None, f"{title!r}: could not write SKUs back to Shopify ({exc}) -- will retry"

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
    if adopted is not None:
        log.info("adopted %r as %s -- its Shopify SKUs were already ours", title, product_id)
    else:
        log.info("imported %r from Shopify as %s (%d variant(s))", title, product_id, len(prepared))
    return entry, None


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
        if set(summary.get("skus") or []) & known_ids:
            # Recognised from the list read alone. The detail call below is
            # the expensive part of this loop, and on a store where nothing
            # has been added it is the *whole* cost -- skipping it is what
            # lets `scheduler` run this on a timer rather than only at boot.
            continue
        detail = admin.get_product(summary["id"])
        if detail is None:
            continue
        entry, problem = _import_one(session, summary, detail, known_ids, apply=apply)
        if entry is not None:
            imported.append(entry)
        if problem is not None:
            problems.append(problem)

    return {"checked": len(summaries), "imported": imported, "problems": problems}


def import_product(session: Session, product_gid: str, *, apply: bool = True) -> dict | None:
    """One product by its Shopify gid, for the `products/create` webhook.

    The reconcile above runs at boot and reads the whole catalogue. That was
    the only door, so a product added in Shopify Admin at 2pm stayed invisible
    to the bot until the next deploy -- which on a good week is never. This is
    the same rules applied to the one product Shopify just told us about,
    without the catalogue read.

    Returns the imported entry, or None when there was nothing to do (already
    known, a placeholder, or an unreadable product). A refusal is logged
    rather than raised: a webhook handler has nobody to raise at.
    """
    summary = admin.get_product(product_gid)
    if summary is None:
        log.info("product %s is not readable; nothing imported", product_gid)
        return None

    known_ids = {row[0] for row in session.execute(select(Variant.variant_id)).all()}
    entry, problem = _import_one(
        session,
        {
            "id": product_gid,
            "title": summary.get("title"),
            "category": summary.get("category"),
            "image_url": summary.get("image_url"),
        },
        summary,
        known_ids,
        apply=apply,
    )
    if problem is not None:
        log.warning("%s", problem)
    return entry
