"""Build `data/products_seed.json` + `data/images/` for Rehla from the scrape.

    python scripts/rehla/build_seed.py [--images ../Rehla/scraped/rehlaeg/images]

Source: `data/rehla_source/products_clean.json` (21 products, 204 variants).
The scraped images are 219 MB of PNG/JPG, several over WhatsApp's 5 MB image
limit, so each is re-encoded once to JPEG, longest side 1280 px -- that copy is
what ships in `data/images/<handle>/`. Re-running is idempotent.

What is decided here rather than taken from the scrape, all in `CURATED`:
category / style / sleeve (the scrape has no product_type on 20 of 21), a
cleaned display name where the store title carries a typo, the colour of
single-colour products the store never gave a Color option, and a price the
scrape caught wrong (the jacket).

SKU (= `variant_id`, the key everything else joins on): the store's own SKU
when it has one and it is unique; otherwise a stable slug built from
handle + colour + size, e.g. `rehla-backless-top-black-m`. The store has one
SKU shared by two variants and four products with none at all.

Stock: the storefront only says available / sold out, so an available variant
seeds 10 and a sold-out one 0.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "data" / "rehla_source" / "products_clean.json"
OUT = REPO / "data" / "products_seed.json"
IMAGES_OUT = REPO / "data" / "images"
DEFAULT_IMAGES = REPO.parent / "Rehla" / "scraped" / "rehlaeg"

IN_STOCK_QTY = 10
MAX_SIDE = 1280

#: Store colour spellings -> the one the bot shows. Typos and emoji only;
#: no colour is merged into another.
COLOR_FIX = {
    "burghandy": "Burgundy",
    "lavendar": "Lavender",
    "violent": "Violet",
    "navy blue": "Navy Blue",
    "baby blue": "Baby Blue",
    "royal blue": "Royal Blue",
    "mint green": "Mint Green",
    "gray": "Gray",
    "grey": "Gray",
}

TOPS, TEES, PANTS, OUTER, CAPS = "Tops", "T-Shirts", "Pants", "Hoodies & Jackets", "Caps"

#: handle -> what the scrape cannot say. `name` only where the store title has
#: a typo or is not a name a customer could repeat back.
CURATED: dict[str, dict] = {
    "rehla-hijabi-off-shoulder-top": dict(
        category=TOPS, style=["off-shoulder", "hijabi", "basic", "fitted"], sleeve="long",
        description="Long-sleeve off-shoulder top, hijab-friendly. Soft stretchy fabric.",
    ),
    "rehla-off-shoulder-top": dict(
        category=TOPS, style=["off-shoulder", "lace"], sleeve="half",
        description="Off-shoulder top with lace detail.",
    ),
    "rehla-backless-top": dict(
        category=TOPS, style=["backless", "halter", "v-neck"], sleeve="sleeveless",
        description="V-neck halter top with an open back.",
    ),
    "wide-leg-flare-trousers": dict(
        name="Rehla Yoga Pants", category=PANTS, style=["wide-leg", "flare", "yoga"],
        sleeve="sleeveless",
    ),
    "rehla-tops": dict(name="Rehla Tops", category=TOPS, style=["basic", "fitted"], sleeve="half"),
    "rehla-orignal-tops": dict(name="Rehla Original Tops", category=TOPS, style=["basic", "fitted"], sleeve="half"),
    "rehlaa-pink-hoodie": dict(
        name="Rehla Pink Hoodie", category=OUTER, style=["hoodie", "pullover"], sleeve="long",
        color="Pink",
    ),
    "rehla-long-sleve-black-top": dict(
        name="Rehla Long Sleeve Top", category=TOPS, style=["basic", "fitted", "long-sleeve"],
        sleeve="long",
    ),
    "rehla-flares-long-sleeves-top": dict(
        name="Rehla Flared Long Sleeve Top", category=TOPS, style=["flared-sleeve", "fitted", "long-sleeve"],
        sleeve="long",
    ),
    "rehla-squared-long-sleeves-top": dict(
        name="Rehla Square Neck Long Sleeve Top", category=TOPS,
        style=["square-neck", "fitted", "long-sleeve"], sleeve="long",
    ),
    "rehla-black-cap": dict(name="Rehla Black Cap", category=CAPS, style=["cap"], sleeve="sleeveless"),
    "rehla-pink-cap": dict(name="Rehla Pink Cap", category=CAPS, style=["cap"], sleeve="sleeveless"),
    "rehla-t-shirt-black-3": dict(name="Rehla T-Shirt Black 3", category=TEES, style=["tee"], sleeve="half"),
    "rehla-printed-t-shirt-2": dict(
        name="Rehla Black T-Shirt 2", category=TEES, style=["tee", "graphic"], sleeve="half", color="Black",
    ),
    "rehla-t-shirt-white-3": dict(
        name="Rehla T-Shirt White 3", category=TEES, style=["tee"], sleeve="half", color="White",
    ),
    "rehla-baby-blue-t-shirt": dict(
        name="Rehla Baby Blue T-Shirt", category=TEES, style=["tee"], sleeve="half", color="Baby Blue",
    ),
    "rehla-half-printed-black-shirt": dict(
        name="Rehla Half Printed Black T-Shirt", category=TEES, style=["tee", "graphic"], sleeve="half",
        color="Black",
    ),
    "t-shirt-white-shirt": dict(
        name="Rehla White T-Shirt", category=TEES, style=["tee"], sleeve="half", color="White",
    ),
    "rehlaa-of-white-hoodie": dict(
        name="Rehla Off White Hoodie", category=OUTER, style=["hoodie", "pullover"], sleeve="long",
        color="Off White",
    ),
    "rehlaa-jacket": dict(
        name="Rehla Jacket", category=OUTER, style=["jacket"], sleeve="long", color="Black",
        # The scrape caught 100 (compare-at 1200); the live store sells it at
        # 1000. The seed is what the bot quotes when Shopify is unreachable.
        price=1000.0,
    ),
    "rehla-black-t-shirt": dict(
        name="Rehla Black T-Shirt", category=TEES, style=["tee"], sleeve="half", color="Black",
    ),
}

#: The size chart each product is answered with -- the numbers live in
#: `data/size_charts.json` and match the store's own `custom.size_chart_data`
#: metafields. The pants and hoodies have no published measurements, only the
#: weight guide every product carries; the caps are one size and have none.
SIZE_CHART_BY_CATEGORY = {TOPS: "rehla-top", TEES: "rehla-tshirt", PANTS: "rehla-weight-guide"}
SIZE_CHART_BY_HANDLE = {
    "rehlaa-jacket": "rehla-jacket",
    "rehlaa-pink-hoodie": "rehla-weight-guide",
    "rehlaa-of-white-hoodie": "rehla-weight-guide",
}

#: Search labels on top of `style`: fit, fabric, length, occasion. They go into
#: `style` (what the catalog search reads) and onto the Shopify product as
#: tags. Arabic synonyms for each live in `domain/services/search_terms.py`.
TAGS_BY_CATEGORY = {
    TOPS: ["top", "women", "everyday", "going-out", "casual", "stretch"],
    TEES: ["tee", "t-shirt", "women", "everyday", "casual", "cotton", "relaxed", "short-sleeve", "regular-length"],
    PANTS: ["pants", "trousers", "women", "high-rise", "full-length", "going-out", "casual"],
    OUTER: ["women", "winter", "casual", "warm"],
    CAPS: ["accessory", "one-size", "casual"],
}
TAGS_BY_HANDLE = {
    "rehla-hijabi-off-shoulder-top": ["modest", "long-sleeve"],
    "rehla-off-shoulder-top": ["short-sleeve", "going-out", "evening"],
    "rehla-backless-top": ["evening", "summer", "sleeveless"],
    "wide-leg-flare-trousers": ["wide-leg", "flare", "mid-weight"],
    "rehla-tops": ["short-sleeve", "baby-tee", "crop", "printed", "logo", "summer", "soft"],
    "rehla-orignal-tops": ["short-sleeve", "baby-tee", "crop", "printed", "logo", "summer", "breathable", "soft"],
    "rehla-long-sleve-black-top": ["long-sleeve", "modest", "elastic"],
    "rehla-flares-long-sleeves-top": ["long-sleeve", "modest", "elastic", "flared-hem"],
    "rehla-squared-long-sleeves-top": ["long-sleeve", "modest", "elastic", "flared-hem"],
    "rehlaa-pink-hoodie": ["hoodie", "pullover", "oversized"],
    "rehlaa-of-white-hoodie": ["hoodie", "pullover", "oversized"],
    "rehlaa-jacket": ["jacket", "zip-up", "cotton", "heavy"],
}


def tags_for(handle: str, category: str, style: list[str]) -> list[str]:
    return list(dict.fromkeys([*style, *TAGS_BY_CATEGORY[category], *TAGS_BY_HANDLE.get(handle, [])]))


SIZE_ORDER = ["XS", "S", "M", "L", "XL", "XXL", "One Size"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def clean_color(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"[^\w\s-]", "", raw, flags=re.UNICODE).strip()
    text = " ".join(text.split())
    return COLOR_FIX.get(text.lower(), text.title() if text.islower() else text)


def _options(product: dict) -> tuple[int | None, int | None]:
    """Which option slot (1-based) holds colour and which holds size."""
    color_idx = size_idx = None
    for i, opt in enumerate(product["options"], start=1):
        name = opt["name"].strip().lower()
        if name in {"color", "colour"}:
            color_idx = i
        elif name == "size" or set(v.upper() for v in opt["values"]) <= set(SIZE_ORDER):
            size_idx = i
    return color_idx, size_idx


def encode_image(src: Path, dst: Path) -> None:
    from PIL import Image

    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[3])
            im = bg
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        im.save(dst, "JPEG", quality=85, optimize=True, progressive=True)


def build(images_root: Path, *, write_images: bool = True) -> list[dict]:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    sku_counts = collections.Counter(v["sku"] for p in source for v in p["variants"] if v.get("sku"))

    def local(path: str | None) -> str | None:
        if not path:
            return None
        rel = Path(path)  # images/<handle>/NN.ext
        out = f"data/images/{rel.parent.name}/{rel.stem}.jpg"
        if write_images:
            encode_image(images_root / rel, REPO / out)
        return out

    seed: list[dict] = []
    for p in source:
        cur = CURATED[p["handle"]]
        color_idx, size_idx = _options(p)
        variants = []
        color_images: dict[str, list[str]] = {}
        seen_ids: set[str] = set()
        for v in p["variants"]:
            color = clean_color(v.get(f"option{color_idx}")) if color_idx else cur.get("color")
            size = (v.get(f"option{size_idx}") or "").strip().upper() if size_idx else "One Size"
            price = float(cur.get("price", v["price"]))
            compare = float(v["compare_at_price"]) if v.get("compare_at_price") else price
            original = max(compare, price)
            sku = (v.get("sku") or "").strip()
            if not sku or sku_counts[sku] > 1:
                sku = slug(f"{p['handle']}-{color or ''}-{size}")
            assert sku not in seen_ids, sku
            seen_ids.add(sku)
            img = local(v.get("image_local_path"))
            if color and img and img not in color_images.setdefault(color, []):
                color_images[color].append(img)
            variants.append(
                {
                    "variant_id": sku,
                    "size": size,
                    "color": color,
                    "length": None,
                    "price": price,
                    "original_price": original,
                    "on_sale": original > price,
                    "stock_qty": IN_STOCK_QTY if v.get("available") else 0,
                    "low_stock_threshold": 2,
                }
            )
        images = [local(i.get("local_path")) for i in p.get("images") or []]
        images = [i for i in images if i]
        colors = list(dict.fromkeys(v["color"] for v in variants if v["color"]))
        if len(colors) == 1 and not color_images.get(colors[0]):
            color_images = {colors[0]: images}
        sizes = sorted({v["size"] for v in variants}, key=lambda s: SIZE_ORDER.index(s) if s in SIZE_ORDER else 99)
        prices = [v["price"] for v in variants]
        description = cur.get("description") or " ".join((p.get("body_text") or "").split())[:400]
        seed.append(
            {
                "product_id": p["handle"],
                "name": cur.get("name") or " ".join(p["title"].split()),
                "category": cur["category"],
                "department": "women",
                "style": tags_for(p["handle"], cur["category"], cur["style"]),
                "collection": None,
                "sleeve": cur["sleeve"],
                "size_chart": SIZE_CHART_BY_HANDLE.get(p["handle"], SIZE_CHART_BY_CATEGORY.get(cur["category"])),
                "sizes": sizes,
                "colors": colors,
                "lengths": [],
                "price": min(prices),
                "original_price": max(v["original_price"] for v in variants),
                "on_sale": any(v["on_sale"] for v in variants),
                "images": images,
                "color_images": color_images,
                "description": description,
                "source_products": [
                    {
                        "handle": p["handle"],
                        "shopify_id": str(p["id"]),
                        "color": None,
                        "url": f"https://rehlaeg.com/products/{p['handle']}",
                    }
                ],
                "variants": variants,
            }
        )
    return seed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=Path, default=DEFAULT_IMAGES,
                    help="folder that holds images/<handle>/ from the scrape")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args()
    seed = build(args.images, write_images=not args.no_images)
    OUT.write_text(json.dumps(seed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    nv = sum(len(p["variants"]) for p in seed)
    stocked = sum(1 for p in seed for v in p["variants"] if v["stock_qty"] > 0)
    print(f"{len(seed)} products, {nv} variants, {stocked} in stock -> {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
