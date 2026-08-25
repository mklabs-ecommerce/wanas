"""
Rebuild products_seed.json from the scraped Shopify export.

What this does that the source data doesn't:

1. **Colour becomes a variant axis, not a product.** The source store lists
   "WANAS BLACK HOODIE", "WANAS GREY HOODIE" and "WANAS OLIVE HOODIE" as three
   products. They are one product in three colours. Merging them collapses
   43 source products into 18 real ones without losing a single variant --
   208 before, 208 after -- and makes the catalog match how a customer shops:
   the hoodie, in olive, in M.

2. **A category taxonomy modelled on ASOS.** Six categories instead of the
   source's eight ad-hoc product types. Crewnecks and quarter-zips are not
   categories anywhere in retail; they sit under Hoodies & Sweatshirts, and
   the difference lives in `style` -- which is a filter, the way ASOS treats
   fit and cut.

3. **Collections are a separate, optional axis.** Only WINTER COLLECTION and
   CAIROKEE MERCH exist. Everything else has no collection. This mirrors the
   ASOS split between "shop by product" and "shop by edit": an edit is a
   merchandising decision that changes every season and must never be load
   bearing for finding a garment.

Run:  python data/merge_catalog.py
"""

import json
import os
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRAPE = Path(
    os.environ.get("WANAS_SCRAPE") or (REPO.parent / "E-commerce")
)

SIZE_VALUES = {"XS", "S", "M", "L", "XL", "XXL"}
DEFAULT_STOCK = 10
LOW_STOCK_THRESHOLD = 2

# Products whose second option axis is a length, not a colour.
LENGTH_OPTION_HANDLES = {"black-worker-jacket", "black-worker-jacket-copy"}

# ---------------------------------------------------------------------------
# The merge map.
#
# Explicit and product-by-product on purpose. Source handles are actively
# misleading -- `olive-sweatpants` is the black one, `wanas-grey-t-shirt` is
# the black tee, `wanas-navy-quartrzip-1` is camel brown -- so any rule that
# derived colour or grouping from the handle would silently mis-file stock.
# A list can be read and checked against the shop; a regex cannot.
#
#   members: source handle -> the colour that product actually is
#            (None when the product already carries its own colour axis)
# ---------------------------------------------------------------------------
GROUPS = [
    {
        "product_id": "ringer-tee",
        "description": 'Ringer tee, boxy fit. Model wears M (70kg, 178cm).',
        "name": "Ringer Tee",
        "category": "T-Shirts",
        "department": "unisex",
        "style": ["boxy-fit", "ringer"],
        "collection": None,
        "size_chart": "ringer-boxy-tee",
        "members": {
            "beige-ringer-tee": "Beige",
            "envy-t-shirt-copy": "Brown",
            "path-to-heaven-t-shirt": "Burgundy",
            "porche-t-shirt": "Navy",
        },
    },
    {
        "product_id": "boxy-wns-tee",
        "description": 'Boxy fit tee. Model wears L (70kg, 178cm).',
        "name": "Boxy WNS Tee",
        "category": "T-Shirts",
        "department": "unisex",
        "style": ["boxy-fit"],
        "collection": None,
        "size_chart": "wns-boxy-tee",
        "members": {
            "wanas-grey-t-shirt": "Black",
            "path-to-heaven-t-shirt-copy": "Grey",
            "wanas-olive-t-shirt-1": "Olive",
        },
    },
    {
        "product_id": "cairokee-tee",
        "description": 'Cairokee band tee, oversized fit.',
        "name": "Cairokee T-shirt",
        "category": "T-Shirts",
        "department": "unisex",
        "style": ["oversized", "graphic"],
        "collection": "CAIROKEE MERCH",
        "size_chart": "oversized-graphic-tee",
        "members": {"cairokee-t-shirt-copy": None},
    },
    {
        "product_id": "cairokee-tee-2",
        "description": 'Cairokee band tee, oversized fit. Female model wears S (56kg, 172cm); male model wears L (93kg, 180cm).',
        "name": "Cairokee T-shirt 2",
        "category": "T-Shirts",
        "department": "unisex",
        "style": ["oversized", "graphic"],
        "collection": "CAIROKEE MERCH",
        "size_chart": "oversized-graphic-tee",
        "members": {"cairokee-t-shirt-2": None},
    },
    {
        "product_id": "envy-tee",
        "description": 'Oversized tee with a dropped shoulder cut. Female model wears S (56kg, 172cm); male model wears L (93kg, 180cm).',
        "name": "Envy T-shirt",
        "category": "T-Shirts",
        "department": "unisex",
        "style": ["oversized", "graphic"],
        "collection": None,
        "size_chart": "oversized-graphic-tee",
        "members": {"envy-t-shirt-1": "Grey"},
    },
    {
        "product_id": "wanas-hoodie",
        "description": 'Oversized pullover hoodie. Model wears M (70kg, 178cm).',
        "name": "WANAS Hoodie",
        "category": "Hoodies & Sweatshirts",
        "department": "unisex",
        "style": ["oversized", "pullover"],
        "collection": "WINTER COLLECTION",
        "size_chart": "oversized-hoodie",
        "members": {
            "wanas-black-hoodie": "Black",
            "wanas-grey-hoodie": "Grey",
            "wanas-olive-hoodie": "Olive",
        },
    },
    {
        "product_id": "wanas-zip-hoodie",
        "description": 'Oversized zip-through hoodie. Model wears M (70kg, 178cm).',
        "name": "WANAS Zip-Hoodie",
        "category": "Hoodies & Sweatshirts",
        "department": "unisex",
        "style": ["oversized", "zip-through"],
        "collection": "WINTER COLLECTION",
        "size_chart": "wanas-zip-hoodie",
        "members": {
            "wanas-black-zip-hoodie": "Black",
            "wanas-grey-zip-hoodie": "Grey",
            "wanas-olive-zip-hoodie": "Olive",
        },
    },
    {
        "product_id": "zipup",
        "description": 'Zip-through hoodie.',
        "name": "Zipup",
        "category": "Hoodies & Sweatshirts",
        "department": "unisex",
        "style": ["zip-through"],
        "collection": "WINTER COLLECTION",
        "size_chart": "zipup",
        "members": {
            "black-zipup-1": "Black",
            "pink-zipup": "Pink",
            "vintage-green-zipup-1": "Vintage Green",
        },
    },
    {
        "product_id": "cairokee-hoodie",
        "description": 'Cairokee band hoodie, oversized pullover fit.',
        "name": "Cairokee Hoodie",
        "category": "Hoodies & Sweatshirts",
        "department": "unisex",
        "style": ["oversized", "graphic", "pullover"],
        "collection": "CAIROKEE MERCH",
        "size_chart": "oversized-hoodie",
        "members": {"cairokee-hoodie": None},
    },
    {
        "product_id": "wanas-crewneck",
        "description": 'Oversized crewneck sweatshirt. Model wears M (60kg, 178cm).',
        "name": "WANAS Crewneck",
        "category": "Hoodies & Sweatshirts",
        "department": "unisex",
        "style": ["oversized", "crewneck"],
        "collection": "WINTER COLLECTION",
        "size_chart": "oversized-crewneck",
        "members": {
            "wanas-burgandy-crewneck": "Burgundy",
            "wanas-navy-crewneck": "Navy",
            "wanas-olive-crewneck": "Olive",
        },
    },
    {
        "product_id": "wanas-quarter-zip",
        "description": 'Oversized quarter-zip sweatshirt. Model wears XL (95kg, 180cm).',
        "name": "WANAS Quarter-Zip",
        "category": "Hoodies & Sweatshirts",
        "department": "unisex",
        "style": ["oversized", "quarter-zip"],
        "collection": "WINTER COLLECTION",
        "size_chart": "quarter-zip",
        "members": {
            "wanas-navy-quartrzip-1": "Camel Brown",
            "wanas-lightbrown-quartrzip": "Light Brown",
            "wanas-navy-quartrzip": "Navy",
        },
    },
    {
        "product_id": "knitted-polo",
        "description": 'Knitted polo, boxy fit. Female model wears M (66kg, 180cm); male model wears M (70kg, 178cm).',
        "name": "Knitted Polo",
        "category": "Polo Shirts",
        "department": "unisex",
        "style": ["boxy-fit", "knitted"],
        "collection": None,
        "size_chart": "knitted-polo",
        "members": {
            "burgandy-knitted-polo": "Burgundy",
            "navy-knitted-polo": "Navy",
            "olive-knitted-polo": "Olive",
            "white-knitted-polo": "White",
        },
    },
    {
        "product_id": "wanas-polo",
        "description": 'Oversized polo. Model wears M (66kg, 182cm).',
        "name": "WANAS Polo",
        "category": "Polo Shirts",
        "department": "unisex",
        "style": ["oversized"],
        "collection": "WINTER COLLECTION",
        "size_chart": "oversized-polo",
        "members": {
            "wanas-black-polo": "Black",
            "wanas-grey-polo": "Grey",
            "wanas-olive-polo": "Olive",
        },
    },
    {
        "product_id": "wanas-sweatpant",
        "description": 'Winter melton sweatpants, wide leg. Model wears S (70kg, 178cm).',
        "name": "WANAS Sweatpant",
        "category": "Joggers & Sweatpants",
        "department": "unisex",
        "style": ["wide-leg"],
        "collection": "WINTER COLLECTION",
        "size_chart": "wide-leg-sweatpants",
        "members": {
            "wanas-black-sweatpant": "Black",
            "wanas-grey-sweatpant": "Grey",
            "wanas-olive-sweatpant": "Olive",
        },
    },
    {
        "product_id": "lightweight-sweatpant",
        "description": 'Lightweight sweatpants, wide leg. Model wears L (90kg, 180cm).',
        "name": "Lightweight Sweatpant",
        "category": "Joggers & Sweatpants",
        "department": "unisex",
        "style": ["wide-leg", "lightweight"],
        "collection": None,
        "size_chart": "wide-leg-sweatpants",
        "members": {
            "olive-sweatpants": "Black",
            "grey-sweatpants-copy": "Grey",
            "black-sweatpants": "Navy",
        },
    },
    {
        "product_id": "worker-jacket",
        "description": 'Oversized worker jacket, available with long or short sleeves. Female model wears M (56kg, 172cm); male model wears L (93kg, 180cm).',
        "name": "Worker Jacket",
        "category": "Jackets",
        "department": "unisex",
        "style": ["oversized", "worker"],
        "collection": None,
        "size_chart": "worker-jacket",
        "members": {
            "black-worker-jacket": "Black",
            "black-worker-jacket-copy": "Olive",
        },
    },
    {
        "product_id": "feelin-fine-top",
        "description": 'Fitted top. Female model wears M (66kg, 180cm).',
        "name": "Feelin Fine Top",
        "category": "Tops",
        "department": "women",
        "style": ["fitted"],
        "collection": None,
        "size_chart": "wns-tops",
        "members": {"white-top": None},
    },
    {
        "product_id": "heart-top",
        "description": 'Fitted top. Female model wears M (66kg, 180cm).',
        "name": "Heart Top",
        "category": "Tops",
        "department": "women",
        "style": ["fitted"],
        "collection": None,
        "size_chart": "wns-tops",
        "members": {"black-top": None},
    },
]

CATEGORY_ORDER = [
    "T-Shirts",
    "Hoodies & Sweatshirts",
    "Polo Shirts",
    "Joggers & Sweatpants",
    "Jackets",
    "Tops",
]


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def tidy_option(value):
    """Shopify option values are inconsistently cased ('olive' vs 'Black')."""
    return value.strip().title()


def classify_options(handle, variants):
    """Work out which option axis holds sizes and which holds colour/length.

    Shopify does not guarantee a consistent axis order -- cairokee-hoodie has
    colour in option1 and size in option2, the reverse of every other product --
    so the axis is identified by its values rather than its position.

    Both failure modes raise rather than degrade. An unrecognised size vocabulary
    silently sets every size to None, which collapses whole families of
    variant_ids into duplicates; a third axis would be silently dropped. Either
    one ships a catalog that looks fine and is wrong.
    """
    axes = {}
    for key in ("option1", "option2", "option3"):
        values = [v[key] for v in variants if v.get(key)]
        if values:
            axes[key] = list(dict.fromkeys(values))

    size_axis = next(
        (k for k, vals in axes.items() if all(v.upper() in SIZE_VALUES for v in vals)),
        None,
    )
    if size_axis is None:
        raise SystemExit(
            f"{handle}: no axis looks like sizes -- values {list(axes.values())}. "
            f"Add the missing size to SIZE_VALUES rather than letting it through."
        )
    if len(axes) > 2:
        raise SystemExit(
            f"{handle}: {len(axes)} option axes, but only size + one other is "
            f"supported. This product needs handling before it can be merged."
        )
    other_axis = next((k for k in axes if k != size_axis), None)
    return size_axis, other_axis


def price_pair(variant):
    """(price, original_price) for one variant, with the stale-compare guard."""
    price = float(variant["price"])
    compare = variant.get("compare_at_price")
    original = float(compare) if compare else price
    # Two products in the source store carry a compare_at_price *below* their
    # actual price -- a stale value left behind when the price was raised.
    # Taken at face value it renders as a negative discount, so the floor is
    # the price itself: those products simply aren't on sale.
    return price, max(original, price)


def build_variants(group, sources, carried):
    """Every buyable combination across the group's member products.

    Colour comes from the merge map for products the store split by colour, and
    from the product's own option axis for products that already had one. Both
    end up in the same place, which is the entire point of the merge.
    """
    out = []
    for handle, mapped_color in group["members"].items():
        raw = sources[handle]
        size_axis, other_axis = classify_options(handle, raw["variants"])
        is_length = handle in LENGTH_OPTION_HANDLES

        for v in raw["variants"]:
            # Sizes are normalised like every other option value. Left raw, a
            # lowercase "m" passes the axis check and then fails to join against
            # size_charts.json, whose keys are S/M/L/XL -- silently breaking
            # every sizing answer for that product.
            size = v[size_axis].strip().upper()
            other = tidy_option(v[other_axis]) if other_axis and v.get(other_axis) else None

            if is_length:
                color, length = mapped_color, other
            else:
                color, length = mapped_color or other, None

            price, original = price_pair(v)
            variant_id = "-".join(
                [group["product_id"]] + [slugify(x) for x in (size, color, length) if x]
            )
            out.append(
                {
                    "variant_id": variant_id,
                    "size": size,
                    "color": color,
                    "length": length,
                    "price": price,
                    "original_price": original,
                    "on_sale": original > price,
                    # Availability in the source store becomes a starting count,
                    # unless a previous seed already has a real number for this
                    # variant -- see carry_forward().
                    "stock_qty": carried.get(
                        variant_id, DEFAULT_STOCK if v["available"] else 0
                    ),
                    "low_stock_threshold": carried.get(
                        variant_id + "\0threshold", LOW_STOCK_THRESHOLD
                    ),
                }
            )
    return out


def carry_forward(old_path):
    """Preserve stock and thresholds across a re-run, keyed by variant_id.

    Without this, regenerating the seed to pick up one new product would reset
    every stock count and threshold the shop had edited since launch -- silently,
    and with a success message.
    """
    if not old_path.exists():
        return {}
    carried = {}
    for p in json.loads(old_path.read_text(encoding="utf-8")):
        for v in p.get("variants", []):
            carried[v["variant_id"]] = v["stock_qty"]
            carried[v["variant_id"] + "\0threshold"] = v["low_stock_threshold"]
    return carried


def build_product(group, sources, carried):
    variants = build_variants(group, sources, carried)

    sizes = list(dict.fromkeys(v["size"] for v in variants if v["size"]))
    colors = list(dict.fromkeys(v["color"] for v in variants if v["color"]))
    lengths = list(dict.fromkeys(v["length"] for v in variants if v["length"]))

    # Photos, kept per colour where we can tell them apart: a customer asking
    # about the olive one should see the olive one, which is only possible
    # because the merge remembers which source product each colour came from.
    #
    # For products the store never split by colour (the Cairokee items, both
    # Tops) the photos are one undifferentiated set, so `color_images` is left
    # empty rather than filled with a guess. Consumers must fall back to
    # `images` -- showing the brown one and calling it black is worse than
    # showing an unlabelled photo.
    color_images, images = {}, []
    for handle, mapped_color in group["members"].items():
        local = [
            str(Path("data") / p).replace("\\", "/")
            for p in sources[handle].get("local_images", [])
        ]
        images.extend(local)
        if mapped_color:
            color_images[mapped_color] = local

    return {
        "product_id": group["product_id"],
        "name": group["name"],
        "category": group["category"],
        "department": group["department"],
        "style": group["style"],
        "collection": group["collection"],
        "size_chart": group["size_chart"],
        # Summaries for display and search. `variants` stays the source of
        # truth for what can actually be bought.
        "sizes": sizes,
        "colors": colors,
        "lengths": lengths,
        "price": min(v["price"] for v in variants),
        "original_price": max(v["original_price"] for v in variants),
        "on_sale": any(v["on_sale"] for v in variants),
        "images": images,
        "color_images": color_images,
        "description": group["description"],
        "source_products": [
            {
                "handle": h,
                "shopify_id": str(sources[h]["id"]),
                "color": c,
                "url": f"https://wanasgallery.myshopify.com/products/{h}",
            }
            for h, c in group["members"].items()
        ],
        "variants": variants,
    }


def check(products, charts):
    """Refuse to write a catalog that is quietly broken.

    Each of these has a silent failure mode: duplicate ids overwrite each other
    in whatever loads the seed, a typo'd chart id makes every sizing answer for
    that product come back empty, and a missing image file only shows up when a
    customer asks to see the product.
    """
    ids = [v["variant_id"] for p in products for v in p["variants"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(f"duplicate variant_id: {sorted(dupes)}")

    for p in products:
        if p["size_chart"] and p["size_chart"] not in charts:
            raise SystemExit(f"{p['product_id']}: unknown size_chart {p['size_chart']}")
        if p["size_chart"]:
            known = set(charts[p["size_chart"]]["sizes"])
            gap = set(p["sizes"]) - known
            if gap:
                raise SystemExit(
                    f"{p['product_id']}: sells sizes {sorted(gap)} that chart "
                    f"{p['size_chart']} does not cover"
                )
        paths = list(p["images"]) + [q for v in p["color_images"].values() for q in v]
        for path in paths:
            if not (REPO / path).exists():
                raise SystemExit(f"{p['product_id']}: missing image {path}")
        if not p["description"]:
            raise SystemExit(f"{p['product_id']}: empty description")


def main():
    if not (SCRAPE / "products.json").exists():
        raise SystemExit(
            f"scrape not found at {SCRAPE}. Set WANAS_SCRAPE to the folder "
            f"holding products.json, categories.csv and images/."
        )
    raw_products = json.loads((SCRAPE / "products.json").read_text(encoding="utf-8"))
    sources = {p["handle"]: p for p in raw_products}

    out_path = REPO / "data" / "products_seed.json"
    carried = carry_forward(out_path)

    # Every source product must land in exactly one group. Without this, adding
    # a product to the store and forgetting to map it means it silently never
    # appears in the catalog.
    mapped = {h for g in GROUPS for h in g["members"]}
    missing = set(sources) - mapped
    if missing:
        raise SystemExit(f"source products not in any group: {sorted(missing)}")
    unknown = mapped - set(sources)
    if unknown:
        raise SystemExit(f"group references unknown handles: {sorted(unknown)}")

    charts = json.loads((REPO / "data" / "size_charts.json").read_text(encoding="utf-8"))

    out = [build_product(g, sources, carried) for g in GROUPS]
    out.sort(key=lambda p: (CATEGORY_ORDER.index(p["category"]), p["name"]))

    # Copy the photos in *before* validating, so a fresh clone that doesn't
    # carry data/images/ can bootstrap. Validating first would fail on files
    # this step is about to create.
    src_images = SCRAPE / "images"
    dst_images = REPO / "data" / "images"
    if src_images.exists():
        shutil.copytree(src_images, dst_images, dirs_exist_ok=True)

    check(out, charts)

    out_path.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    variants = [v for p in out for v in p["variants"]]
    print(f"wrote {len(out)} products to {out_path}")
    print(f"  merged from:       {len(sources)} source products")
    print(f"  variants:          {len(variants)} ({sum(1 for v in variants if v['stock_qty'])} in stock)")
    print(f"  categories:        {len(CATEGORY_ORDER)}")
    print(f"  in a collection:   {sum(1 for p in out if p['collection'])}")
    print(f"  images:            {sum(len(p['images']) for p in out)}")


if __name__ == "__main__":
    main()
