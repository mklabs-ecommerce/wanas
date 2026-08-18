"""Catalog reads: categories, product search, variants.

Every price a customer could ever be told is computed here from the live
variant rows. The product's own `price` / `original_price` columns are never
handed upward as a quotable number.

Since the store moved to Shopify, "live" means live on Shopify. Price, discount
and stock are read from the store at the moment the customer asks, and the
matching wanas.db columns are only a fallback for when Shopify cannot be
reached -- see `_overlay`. Everything else on this page (category, style,
department, collection, description, images) still comes from wanas.db, which
is the only place it exists.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.models import Product, Variant
from backend.money import money
from backend.services import search_terms, shopify_catalog

#: Categories come first and collections last, labelled optional: 8 of the 18
#: products have no collection, so a reply that opens with collections has
#: hidden nearly half the shop.
CATEGORY_ORDER = [
    "T-Shirts",
    "Hoodies & Sweatshirts",
    "Polo Shirts",
    "Joggers & Sweatpants",
    "Jackets",
    "Tops",
]


def get_categories(session: Session) -> dict:
    counts = dict(
        session.execute(select(Product.category, func.count()).group_by(Product.category)).all()
    )
    categories = [
        {"category": name, "product_count": counts[name]}
        for name in CATEGORY_ORDER
        if name in counts
    ]
    for name, count in sorted(counts.items()):
        if name not in CATEGORY_ORDER:
            categories.append({"category": name, "product_count": count})

    styles: set[str] = set()
    for (raw,) in session.execute(select(Product.style)).all():
        styles.update(raw or [])

    departments = [
        row[0]
        for row in session.execute(select(Product.department).distinct().order_by(Product.department))
    ]
    collections = [
        row[0]
        for row in session.execute(
            select(Product.collection).where(Product.collection.is_not(None)).distinct().order_by(Product.collection)
        )
    ]

    return {
        "categories": categories,
        "styles": sorted(styles),
        "departments": departments,
        "collections": collections,
    }


class _Priced:
    """One variant's price and stock as they should be quoted right now.

    A tiny shim rather than mutating the ORM rows: writing Shopify's numbers
    onto a `Variant` would leave them sitting in the session, and something
    downstream would eventually flush a price the shop never set into
    wanas.db.
    """

    __slots__ = ("price", "original_price", "on_sale", "stock_qty", "live", "image_url")

    def __init__(self, price, original_price, on_sale, stock_qty, live, image_url=None):
        self.price = price
        self.original_price = original_price
        self.on_sale = on_sale
        self.stock_qty = stock_qty
        self.live = live
        self.image_url = image_url


def _overlay(variant: Variant, live_map) -> _Priced:
    """Shopify's numbers where we have them, wanas.db's where we do not.

    Three cases, and they are not the same failure:
      - Shopify unreachable (live_map is None): every variant falls back, which
        is logged once in shopify_catalog rather than 208 times here.
      - Shopify reached, variant absent: its SKU was never written, or the
        product was deleted from the store. Falls back, but this one is worth
        noticing -- run scripts/shopify_set_skus.py.
      - Product archived or draft on Shopify: it is not for sale, whatever
        wanas.db says. Stock reads zero so the bot offers alternatives instead
        of taking an order the storefront would refuse.

    `image_url` rides along the same lookup: None whenever Shopify was not
    reached, the variant is not there, or staff never attached a photo --
    `get_variants` treats a None the same as any other cache miss and keeps
    the local file.
    """
    if live_map is None:
        return _Priced(
            variant.price, variant.original_price, bool(variant.on_sale), variant.stock_qty, False
        )

    live = live_map.get(variant.variant_id)
    if live is None:
        return _Priced(
            variant.price, variant.original_price, bool(variant.on_sale), variant.stock_qty, False
        )

    return _Priced(
        live.price,
        live.original_price,
        live.on_sale,
        live.stock_qty if live.product_active else 0,
        True,
        image_url=live.image_url,
    )


def _product_summary(product: Product, live_map=None) -> dict:
    variants = [_overlay(v, live_map) for v in product.variants]
    prices = [v.price for v in variants]
    originals = [v.original_price for v in variants]
    return {
        "product_id": product.product_id,
        "name": product.name,
        "category": product.category,
        "style": list(product.style or []),
        "department": product.department,
        "collection": product.collection,
        # The full run including sold-out ones: these describe the product,
        # they are not an offer. get_variants decides what can be offered.
        "colors": list(product.colors or []),
        "sizes": list(product.sizes or []),
        "lengths": list(product.lengths or []),
        # min and max of the variants' *current* prices, computed here rather
        # than read from the product row. The WANAS Hoodie is 650 in black and
        # olive but 700 in grey; a single product-level number would have the
        # model quote 650 for a hoodie the customer is charged 700 for, at the
        # door, in cash.
        "price_from": money(min(prices)) if prices else 0,
        "price_to": money(max(prices)) if prices else 0,
        # The highest pre-discount price -- a strike-through number, not the
        # top of a range. Not to be confused with price_to.
        "original_price_to": money(max(originals)) if originals else 0,
        "on_sale": any(v.on_sale for v in variants),
        "any_in_stock": any(v.stock_qty > 0 for v in variants),
        "description": product.description,
    }


def _haystack(product: Product) -> str:
    """Everything about a product one free-text search may look at."""
    return " ".join(
        [
            product.name,
            product.category,
            product.department,
            " ".join(product.style or []),
            " ".join(product.colors or []),
            " ".join(product.sizes or []),
            product.collection or "",
            product.description or "",
        ]
    )


def _matches_query(product: Product, needle: str) -> bool:
    """Free text against name, category, style and variant colours together.

    That combination is what resolves "الهودي الزيتي" now that olive is a
    colour rather than part of a product name.

    The matching itself lives in `search_terms`, which folds Arabic spelling
    variants, maps Arabic and franco words onto the English the catalog is
    actually written in, and drops the padding a spoken request carries. The
    catalog holds no Arabic at all, so without that layer a query typed the way
    a customer types it matches nothing -- and "we don't have it" about
    something on the shelf is the most expensive wrong answer this shop can
    give.
    """
    return search_terms.matches(_haystack(product), needle)


def get_products(
    session: Session,
    *,
    category: str | None = None,
    style: str | None = None,
    department: str | None = None,
    collection: str | None = None,
    query: str | None = None,
) -> dict:
    stmt = select(Product).options(selectinload(Product.variants)).order_by(Product.name)
    if category:
        stmt = stmt.where(func.lower(Product.category) == category.lower())
    if department:
        stmt = stmt.where(func.lower(Product.department) == department.lower())
    if collection:
        stmt = stmt.where(func.lower(Product.collection) == collection.lower())

    products = list(session.scalars(stmt).all())

    if style:
        wanted = style.lower()
        products = [p for p in products if any(wanted == s.lower() for s in (p.style or []))]
    if query:
        products = [p for p in products if _matches_query(p, query)]

    live_map = shopify_catalog.live_map()
    summaries = [_product_summary(p, live_map) for p in products]
    return {"products": summaries, "count": len(summaries)}


def _status_for(variant: Variant, priced: _Priced) -> str:
    """`status` is the only stock word a reply may repeat, so it has to follow
    the live number, not the row's own cached one."""
    if priced.stock_qty <= 0:
        return "sold_out"
    if priced.stock_qty <= (variant.low_stock_threshold or 0):
        return "low_stock"
    return "in_stock"


def variant_payload(variant: Variant, live_map=None) -> dict:
    priced = _overlay(variant, live_map)
    return {
        "variant_id": variant.variant_id,
        "size": variant.size,
        "color": variant.color,
        "length": variant.length,
        "price": money(priced.price),
        "original_price": money(priced.original_price),
        "on_sale": bool(priced.on_sale),
        # Returned to the model but never quoted to a customer -- exact counts
        # invite haggling and go stale. `status` is what a reply may reference.
        "stock_qty": priced.stock_qty,
        "status": _status_for(variant, priced),
    }


def _overlay_images(
    product: Product, variants: list[Variant], live_map
) -> tuple[list[str], dict[str, list[str]]]:
    """Shopify's own photo for a colour, when staff have set one, ahead of the
    file `wanas.db` was seeded with -- see the module note in
    `shopify_catalog`. Only the lead photo is ever replaced; whatever gallery
    wanas.db already has for that colour still follows behind it, so "show me
    another angle" still has something to reach for.

    A colour only ever gets a Shopify photo swapped in if `color_images`
    already had an entry for it -- never a new key. `merge_catalog.py` keeps
    that dict all-or-nothing per product (every colour or none), and a partial
    one would make `_candidate_images` (chatbot/tools/base.py) show a photo for
    some colours and silently drop the rest of the product's gallery for
    everyone else. Products with no colour split at all (`color_images` is
    empty) overlay onto the shared `images` list instead, on the same logic:
    the local set is already undifferentiated, so a colour is not a key worth
    inventing here either.
    """
    color_images = {color: list(paths) for color, paths in (product.color_images or {}).items()}
    images = list(product.images or [])

    for variant in variants:
        url = _overlay(variant, live_map).image_url
        if not url:
            continue
        if variant.color and variant.color in color_images:
            gallery = [p for p in color_images[variant.color] if p != url]
            color_images[variant.color] = [url, *gallery]
        elif not color_images:
            images = [url, *(p for p in images if p != url)]

    return images, color_images


def get_variants(session: Session, product_id: str) -> dict | None:
    product = session.get(Product, product_id)
    if product is None:
        return None

    live_map = shopify_catalog.live_map()
    variants = sorted(product.variants, key=lambda v: (v.color or "", v.length or "", v.size))
    stock = {v.variant_id: _overlay(v, live_map).stock_qty for v in variants}
    images, color_images = _overlay_images(product, variants, live_map)
    return {
        "product_id": product.product_id,
        "name": product.name,
        "description": product.description,
        "has_size_chart": product.size_chart is not None,
        # Sold-out variants are returned too, so the bot can say "XL only comes
        # in Black" rather than pretending the combination never existed.
        "variants": [variant_payload(v, live_map) for v in variants],
        "in_stock": [v.variant_id for v in variants if stock[v.variant_id] > 0],
        "images": images,
        # May be empty for the five products the store never split by colour.
        # An unlabelled photo is fine; the wrong colourway labelled
        # confidently is not.
        "color_images": color_images,
    }


def alternatives_for(session: Session, variant: Variant, limit: int = 6) -> list[dict]:
    """In-stock siblings of the same product: same colour in other sizes first,
    then the same size in other colours.

    These are the only substitutes the model may offer -- anything else would
    be invented -- so the ordering is part of the contract, not a nicety.

    Stock is the live figure: offering a replacement for a sold-out item and
    having *that* turn out to be sold out too is the same failure twice in one
    conversation.
    """
    live_map = shopify_catalog.live_map()
    siblings = [
        v
        for v in variant.product.variants
        if v.variant_id != variant.variant_id and _overlay(v, live_map).stock_qty > 0
    ]
    same_colour = [v for v in siblings if v.color == variant.color]
    same_size = [v for v in siblings if v.size == variant.size and v.color != variant.color]
    rest = [v for v in siblings if v not in same_colour and v not in same_size]

    ordered = same_colour + same_size + rest
    return [
        {"variant_id": v.variant_id, "size": v.size, "color": v.color, "length": v.length}
        for v in ordered[:limit]
    ]
