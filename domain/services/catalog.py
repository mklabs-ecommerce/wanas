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

from common.money import money
from common.sizes import in_order, sort_key
from domain.models import Product, Variant
from domain.services import search_terms, sleeves
from integrations.shopify import catalog as shopify_catalog

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

    #: Only the values actually recorded on a product. A facet offered but
    #: empty is a filter the model will use and get nothing back from, which
    #: reads to the customer as "we don't have any".
    sleeve_values = {
        sleeves.effective(row[0], row[1])
        for row in session.execute(select(Product.sleeve, Product.category))
    }

    return {
        "categories": categories,
        "styles": sorted(styles),
        "departments": departments,
        "collections": collections,
        "sleeves": [s for s in sleeves.SLEEVES if s in sleeve_values],
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


def quoted(variant: Variant, live_map=None) -> _Priced:
    """One variant's price and stock as a customer may be told them now.

    The overlay every quote goes through -- `get_variants`, the cart, and
    `place_order`'s own live read -- exposed for a caller that holds a
    `Variant` rather than a payload. Pass the turn's `shopify_catalog.live_map()`
    so a payload of many lines reads the shelf once.
    """
    return _overlay(variant, live_map)


def live_stock(variant: Variant) -> tuple[int, bool]:
    """How many of this variant are really sellable, and whether Shopify said so.

    The same overlay `get_variants` quotes from, exposed for the one caller
    that has a `Variant` in hand and a decision to make about it rather than a
    payload to build. `variant.stock_qty` on its own is a wanas.db column that
    nothing keeps current -- reading it directly is how `add_to_cart` came to
    refuse an item the storefront was happily selling, and (because a refusal
    is what joins the stock waitlist) how a customer was then told it had come
    "back in stock" without a single unit having moved.

    The second element is `False` when Shopify could not be reached and the
    number is wanas.db's own guess. Refusing on it is safe -- an over-cautious
    "sold out" never oversells -- but *acting* on it as though it were
    observed fact is not.
    """
    priced = _overlay(variant, shopify_catalog.live_map())
    return priced.stock_qty, priced.live


def _product_summary(product: Product, live_map=None) -> dict:
    variants = [_overlay(v, live_map) for v in product.variants]
    prices = [v.price for v in variants]
    originals = [v.original_price for v in variants]
    # Same overlay pass as every other live number here, so no second Shopify
    # call: which colourways have at least one buyable size right now.
    stocked = {
        v.color
        for v, priced in zip(product.variants, variants, strict=True)
        if v.color and priced.stock_qty > 0
    }
    return {
        "product_id": product.product_id,
        "name": product.name,
        "category": product.category,
        "style": list(product.style or []),
        "department": product.department,
        "collection": product.collection,
        # `half`, `long` or `sleeveless` -- never null. A product that
        # somehow reaches here unset is answered from its category rather than
        # with a shrug; see `sleeves.effective`.
        "sleeve": sleeves.effective(product.sleeve, product.category),
        # The full run including sold-out ones: these describe the product,
        # they are not an offer. get_variants decides what can be offered.
        "colors": list(product.colors or []),
        "sizes": in_order(product.sizes or []),
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
        # The offer list, ordered like `colors` but only with the colourways a
        # size can actually be bought in right now. `colors` describes the
        # product; this is the slice of it that is for sale.
        "in_stock_colors": [c for c in (product.colors or []) if c in stocked],
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
            # «نص كم» reaches here as `half sleeve` (search_terms), so the
            # words have to be in the text being searched or the fold has
            # nothing to land on.
            sleeves.SEARCH_TEXT.get(sleeves.effective(product.sleeve, product.category), ""),
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


def _named_in_full(products: list[Product], query: str) -> list[Product]:
    """The products a query names by their whole name, or [] when it names none.

    The search is deliberately loose -- every token of the query has to match
    somewhere -- so "Cairokee T-shirt" also finds "Cairokee T-shirt 2", whose
    name contains every word of it. That turned a customer who named one
    product exactly into a "which of these two?" question, asked back about
    something they had already said; it failed the live suite on `main` and on
    this branch alike. A query that contains a product's full name, as whole
    words, is about that product. When one named product's name sits inside
    another named one ("Cairokee T-shirt" inside "Cairokee T-shirt 2"), the
    longer name is the one the query spelt out.
    """
    wanted = f" {search_terms.normalize(query)} "
    taken: list[tuple[int, int]] = []
    named: list[Product] = []
    for product in sorted(products, key=lambda p: len(p.name or ""), reverse=True):
        name = f" {search_terms.normalize(product.name)} "
        if not name.strip():
            continue
        start = wanted.find(name)
        while start != -1:
            end = start + len(name)
            # Only a mention of its own counts -- not the front of a longer
            # name already matched at the same place.
            if all(end <= a or start >= b for a, b in taken):
                taken.append((start, end))
                named.append(product)
                break
            start = wanted.find(name, start + 1)
    return [p for p in products if p in named]


def _resolve_categories(session: Session, given: str) -> list[str]:
    """The real category names a `category` argument means, in order of trust.

    The filter used to be `lower(category) == lower(given)`, which is exact,
    and the model writes this argument from memory: `category="hoodies"` is
    not `Hoodies & Sweatshirts`, so the whole shelf vanished *before* any
    other filter ran and the reply came back "we don't have any". That is the
    same wrong answer `search_terms` exists to prevent, one layer up -- and it
    is worse here, because a zero-row equality filter looks exactly like an
    empty shop.

    Exact first, always: "T-Shirts" has to mean T-Shirts and not every
    category with the word "shirts" in it. Only when nothing matches exactly
    is the name folded through the same matcher the free-text search uses, so
    "hoodies" and «هودي» both reach `Hoodies & Sweatshirts`. An empty list
    means it resolved to no category at all, and the caller treats it as free
    text rather than as a filter that empties the shop.
    """
    names = [row[0] for row in session.execute(select(Product.category).distinct())]
    wanted = " ".join(str(given).split()).casefold()
    exact = [name for name in names if name.casefold() == wanted]
    if exact:
        return exact
    return [name for name in names if search_terms.matches(name, given)]


def get_products(
    session: Session,
    *,
    category: str | None = None,
    style: str | None = None,
    department: str | None = None,
    collection: str | None = None,
    sleeve: str | None = None,
    query: str | None = None,
) -> dict:
    #: An archived product is one the shop no longer sells. Its rows stay --
    #: `order_items` points at them -- but nothing that could lead to a sale
    #: may see it, and search is the first of those.
    stmt = (
        select(Product)
        .options(selectinload(Product.variants))
        .where(Product.archived.is_(False))
        .order_by(Product.name)
    )
    resolved_categories = _resolve_categories(session, category) if category else []
    if resolved_categories:
        stmt = stmt.where(
            func.lower(Product.category).in_([c.lower() for c in resolved_categories])
        )
    if department:
        stmt = stmt.where(func.lower(Product.department) == department.lower())
    if collection:
        stmt = stmt.where(func.lower(Product.collection) == collection.lower())

    products = list(session.scalars(stmt).all())

    if category and not resolved_categories:
        # Not a category this shop has. Used as free text rather than as an
        # equality filter, because "the word you typed is not one of our six
        # category names" must never be served to the customer as "we have
        # nothing like that".
        products = [p for p in products if _matches_query(p, category)]
    if style:
        wanted = style.lower()
        products = [p for p in products if any(wanted == s.lower() for s in (p.style or []))]
    if query:
        products = [p for p in products if _matches_query(p, query)]
        if len(products) > 1:
            products = _named_in_full(products, query) or products

    #: Applied last, and in Python, so it compares the same effective value
    #: the payload goes on to quote. Folded through `sleeves.normalise` rather
    #: than compared raw: the model picks the wording of this argument itself
    #: and sends "short sleeve" as readily as "half". An unrecognised word
    #: filters nothing away, which is the safe direction -- the alternative is
    #: answering "we have none" about a shelf full of them.
    wanted = sleeves.normalise(sleeve) if sleeve else None
    if wanted is not None:
        products = [p for p in products if sleeves.effective(p.sleeve, p.category) == wanted]

    live_map = shopify_catalog.live_map()
    summaries = [_product_summary(p, live_map) for p in products]
    result = {"products": summaries, "count": len(summaries)}
    if len(products) == 1:
        # A search that lands on exactly one product *is* an answer about that
        # product, and a clothes shop answering one in words alone is the one
        # thing it cannot afford. The photos ride under an internal key the
        # tool layer pops, so they steer the attachment without ever reaching
        # the model as paths to describe -- and only for a single match,
        # because a browse across six products is a list, not a showing.
        only = products[0]
        images, color_images = _overlay_images(only, list(only.variants), live_map)
        result["_photo_of"] = {
            "product_id": only.product_id,
            "name": only.name,
            "images": images,
            "color_images": color_images,
        }
    # No `sleeve_unrecorded` any more, and its absence is the point. It
    # existed to tell "we don't sell one" apart from "nobody wrote it down",
    # which was a real distinction while products could be unset. They cannot
    # be: every product answers `half`, `long` or `sleeveless`, so an empty
    # result under a sleeve filter now means exactly what it says.
    return result


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
    """Which photo belongs to which colourway, Shopify's answer preferred.

    Two regimes, and which one applies is decided by *coverage* -- whether
    Shopify has a photo for every colourway the product comes in:

    **Full coverage.** Shopify knows the whole colour split, so it is the
    photo set: `color_images` is rebuilt from it and the seeded
    `data/images/` paths for those colours are dropped rather than trailing
    behind. They are a snapshot of an older catalog and the shop has been
    re-photographed since -- three of the RINGER TEE's four colourways were
    seeded with another product's folder entirely, so "show me another angle
    of the navy one" answered with a photo of a shirt this shop no longer
    sells. A wrong photo is not a spare angle. What `more_images` reaches for
    instead is the other colourways, which is what its own docstring in
    `assistant/tools/base.py` says it is for, and those are now all correct.

    **Partial or no coverage.** Exactly the older, additive behaviour: a
    Shopify photo leads the local gallery for a colour `color_images`
    already had, and never opens a new key. A partial split would make
    `_candidate_images` show a photo for some colours and silently drop the
    rest of the product's gallery for everyone else. A product with no local
    split at all overlays onto the shared `images` list on the same logic.

    Shopify unreachable is the same as no coverage: wanas.db's own photos,
    unchanged.
    """
    color_images = {color: list(paths) for color, paths in (product.color_images or {}).items()}
    images = list(product.images or [])

    # Ordered-unique per colour, commonest first. Every size of one colourway
    # normally carries the same photo, so a majority vote is what makes the
    # lead robust: HEART TOP's large olive has the black photo attached to it
    # in Shopify Admin, and taking whichever variant sorted first would have
    # answered "the olive one" with a picture of the black one.
    counts: dict[str, dict[str, int]] = {}
    for variant in variants:
        url = _overlay(variant, live_map).image_url
        if not (url and variant.color):
            continue
        tally = counts.setdefault(variant.color, {})
        tally[url] = tally.get(url, 0) + 1

    live_by_color = {
        color: sorted(tally, key=lambda url: -tally[url]) for color, tally in counts.items()
    }

    colors = {v.color for v in variants if v.color}
    if colors and colors <= set(live_by_color):
        # Ordered like `product.colors` so the gallery reads in the order the
        # rest of the payload lists the colourways in.
        ordered = [c for c in (product.colors or []) if c in live_by_color]
        ordered += [c for c in live_by_color if c not in ordered]
        color_images = {c: list(live_by_color[c]) for c in ordered}
        images = []
        for gallery in color_images.values():
            images.extend(p for p in gallery if p not in images)
        return images, color_images

    for color, gallery in live_by_color.items():
        url = gallery[0]
        if color in color_images:
            color_images[color] = [url, *(p for p in color_images[color] if p != url)]
        elif not color_images:
            images = [url, *(p for p in images if p != url)]

    return images, color_images


def get_variants(session: Session, product_id: str) -> dict | None:
    product = session.get(Product, product_id)
    # Archived reads as gone here, not as a product with no stock: the bot
    # must not describe, price or sell something the shop has withdrawn.
    if product is None or product.archived:
        return None

    live_map = shopify_catalog.live_map()
    # Size order, not alphabetical order. `sorted` on the bare string gives
    # L, M, S, XL, which is the order the model then recited them in -- see
    # `common/sizes.py`.
    variants = sorted(
        product.variants, key=lambda v: (v.color or "", v.length or "", sort_key(v.size))
    )
    stock = {v.variant_id: _overlay(v, live_map).stock_qty for v in variants}
    images, color_images = _overlay_images(product, variants, live_map)
    return {
        "product_id": product.product_id,
        "name": product.name,
        "description": product.description,
        # The answer to "is this one half sleeve?" -- a property of the
        # product, so it rides with every variant read rather than needing a
        # search. Always one of the three; never null.
        "sleeve": sleeves.effective(product.sleeve, product.category),
        "has_size_chart": product.size_chart is not None or product.size_chart_image is not None,
        # Sold-out variants are returned too, so the bot can say "XL only comes
        # in Black" rather than pretending the combination never existed.
        "variants": [variant_payload(v, live_map) for v in variants],
        "in_stock": [v.variant_id for v in variants if stock[v.variant_id] > 0],
        # The same rows, already added up per colourway -- see `_by_color`.
        "by_color": _by_color(variants, live_map),
        "images": images,
        # May be empty for the five products the store never split by colour.
        # An unlabelled photo is fine; the wrong colourway labelled
        # confidently is not.
        "color_images": color_images,
    }


def _by_color(variants: list[Variant], live_map) -> dict[str, dict]:
    """What can be bought in each colourway, worked out here.

    `variants` is a flat list of 10 to 24 rows, and "which sizes are there in
    olive, and what does olive cost" was the model's to answer by reading all
    of them -- matching each id in `in_stock` back to its size, grouping by
    colour, putting the sizes in S-to-XL order because the prompt says to, and
    noticing that one colour of the Ringer tee is cheaper. Each of those steps
    is a place a sold-out size gets offered or the wrong price gets quoted,
    and none of them needs language understanding.

    Keyed by colour, or by "colour / length" for the Worker Jacket, whose
    length is a third axis. `available` and `sold_out` are sizes in size
    order; the price is one number when the colourway has one, and a
    `price_from` / `price_to` pair when its sizes differ.
    """
    groups: dict[str, list[tuple[Variant, _Priced]]] = {}
    for variant in variants:
        key = variant.color or ""
        if variant.length:
            key = f"{key} / {variant.length}" if key else variant.length
        groups.setdefault(key, []).append((variant, _overlay(variant, live_map)))

    summary: dict[str, dict] = {}
    for key, rows in groups.items():
        available = in_order([v.size for v, priced in rows if priced.stock_qty > 0 and v.size])
        sold_out = in_order(
            [v.size for v, priced in rows if priced.stock_qty <= 0 and v.size and v.size not in available]
        )
        prices = sorted({priced.price for _v, priced in rows})
        originals = sorted({priced.original_price for _v, priced in rows})
        entry: dict = {"available": available, "sold_out": sold_out}
        if len(prices) == 1:
            entry["price"] = money(prices[0])
        else:
            entry["price_from"] = money(prices[0])
            entry["price_to"] = money(prices[-1])
        if any(priced.on_sale for _v, priced in rows):
            entry["original_price"] = money(originals[-1])
        summary[key] = entry
    return summary


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
