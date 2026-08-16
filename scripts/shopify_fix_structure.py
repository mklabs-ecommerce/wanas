"""The two catalog gaps shopify_sync.py deliberately refuses to touch.

shopify_sync.py corrects values on variants that already exist. These two
jobs change what exists, which is a different risk profile, so they live here
and run separately:

  Worker Jacket  wanas.db carries a Long/Short sleeve dimension that never
                 made it to Shopify, so 8 variants stand in for 16. Adding an
                 option restructures the product's variants.

  Zipup          absent from Shopify entirely. Created here with its 12
                 variants and its 7 photos.

Both are idempotent: re-running skips work already done.

    python scripts/shopify_fix_structure.py               # dry run
    python scripts/shopify_fix_structure.py --apply

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import sys
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shopify_sync import ROOT, Shopify, load_env, probe_location, read_db  # noqa: E402

JACKET_TITLE = "WORKER JACKET"
ZIPUP_ID = "zipup"


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------

FIND_PRODUCT = """
query($q: String!) {
  products(first: 10, query: $q) {
    nodes {
      id title
      options { id name optionValues { id name } }
      variants(first: 100) {
        nodes {
          id
          selectedOptions { name value }
          inventoryItem { id }
        }
      }
    }
  }
}
"""


def find_product(gql: Shopify, title: str) -> dict | None:
    for node in gql(FIND_PRODUCT, {"q": f"title:'{title}'"})["products"]["nodes"]:
        if node["title"] == title:
            return node
    return None


def money(v) -> str:
    return f"{v:.2f}"


# --------------------------------------------------------------------------
# Worker Jacket: add the Length option
# --------------------------------------------------------------------------

OPTIONS_CREATE = """
mutation($productId: ID!, $options: [OptionCreateInput!]!) {
  productOptionsCreate(
    productId: $productId
    options: $options
    variantStrategy: CREATE
  ) {
    product { id options { name optionValues { name } } }
    userErrors { field message code }
  }
}
"""

VARIANTS_BULK_UPDATE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    userErrors { field message }
  }
}
"""

INVENTORY_SET = """
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) { userErrors { field message } }
}
"""


def fix_worker_jacket(gql, db_variants, loc, apply: bool) -> None:
    print("\nWORKER JACKET\n" + "-" * 13)
    product = find_product(gql, JACKET_TITLE)
    if not product:
        print(f"  not found on Shopify (looked for {JACKET_TITLE!r}) — skipped")
        return

    names = [o["name"] for o in product["options"]]
    dbvs = db_variants[
        next(k for k in db_variants if k == "worker-jacket")
    ]
    lengths = sorted({v["length"] for v in dbvs if v["length"]})

    if any(n.lower() == "length" for n in names):
        print(f"  Length option already present ({', '.join(names)})")
    else:
        print(f"  options now: {', '.join(names)}")
        print(f"  adding: Length = {', '.join(lengths)}")
        print(f"  variants: {len(product['variants']['nodes'])} -> {len(dbvs)}")
        if not apply:
            print("  (dry run)")
            return
        gql(
            OPTIONS_CREATE,
            {
                "productId": product["id"],
                "options": [
                    {
                        "name": "Length",
                        "values": [{"name": lv} for lv in lengths],
                    }
                ],
            },
        )
        print("  Length option added")
        product = find_product(gql, JACKET_TITLE)

    # Every variant now needs the price and stock the catalog says it has.
    by_key = {
        (v["size"], v["color"], v["length"]): v for v in dbvs
    }
    price_updates, qty_updates, unmatched = [], [], []
    for sv in product["variants"]["nodes"]:
        opts = {o["name"].lower(): o["value"] for o in sv["selectedOptions"]}
        key = (opts.get("size"), opts.get("color"), opts.get("length"))
        dbv = by_key.get(key)
        if not dbv:
            unmatched.append(key)
            continue
        price_updates.append(
            {
                "id": sv["id"],
                "price": money(dbv["price"]),
                **(
                    {"compareAtPrice": money(dbv["original_price"])}
                    if dbv["on_sale"]
                    else {}
                ),
            }
        )
        qty_updates.append(
            {
                "inventoryItemId": sv["inventoryItem"]["id"],
                "locationId": loc,
                "quantity": dbv["stock_qty"],
            }
        )

    for key in unmatched:
        print(f"  ! variant {key} has no match in wanas.db — left alone")

    print(f"  prices to set: {len(price_updates)}, stock to set: {len(qty_updates)}")
    if not apply:
        print("  (dry run)")
        return
    if price_updates:
        gql(VARIANTS_BULK_UPDATE, {"productId": product["id"], "variants": price_updates})
        print(f"  prices set on {len(price_updates)} variant(s)")
    if qty_updates:
        gql(
            INVENTORY_SET,
            {
                "input": {
                    "name": "available",
                    "reason": "correction",
                    "ignoreCompareQuantity": True,
                    "quantities": qty_updates,
                }
            },
        )
        print(f"  stock set on {len(qty_updates)} variant(s)")


# --------------------------------------------------------------------------
# Zipup: create the product
# --------------------------------------------------------------------------

PRODUCT_CREATE = """
mutation($product: ProductCreateInput!) {
  productCreate(product: $product) {
    product { id options { name optionValues { name } } }
    userErrors { field message }
  }
}
"""

VARIANTS_BULK_CREATE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(
    productId: $productId
    variants: $variants
    strategy: REMOVE_STANDALONE_VARIANT
  ) {
    productVariants { id selectedOptions { name value } inventoryItem { id } }
    userErrors { field message }
  }
}
"""

STAGED_UPLOAD = """
mutation($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""

CREATE_MEDIA = """
mutation($productId: ID!, $media: [CreateMediaInput!]!) {
  productCreateMedia(productId: $productId, media: $media) {
    mediaUserErrors { field message }
  }
}
"""


def post_multipart(url: str, fields: list[tuple[str, str]], filename: str, blob: bytes):
    """Shopify's staged upload target takes a plain multipart POST."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields:
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode()
    body += blob + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status not in (200, 201, 204):
            raise RuntimeError(f"upload returned HTTP {r.status}")


def upload_images(gql, product_id: str, paths: list[Path]) -> None:
    inputs = [
        {
            "resource": "IMAGE",
            "filename": p.name if p.parent.name == "" else f"{p.parent.name}-{p.name}",
            "mimeType": mimetypes.guess_type(p.name)[0] or "image/jpeg",
            "httpMethod": "POST",
            "fileSize": str(p.stat().st_size),
        }
        for p in paths
    ]
    targets = gql(STAGED_UPLOAD, {"input": inputs})["stagedUploadsCreate"]["stagedTargets"]

    media = []
    for path, target in zip(paths, targets):
        post_multipart(
            target["url"],
            [(p["name"], p["value"]) for p in target["parameters"]],
            f"{path.parent.name}-{path.name}",
            path.read_bytes(),
        )
        media.append(
            {
                "originalSource": target["resourceUrl"],
                "mediaContentType": "IMAGE",
                "alt": path.parent.name.replace("-", " "),
            }
        )
        print(f"    uploaded {path.parent.name}/{path.name}")

    gql(CREATE_MEDIA, {"productId": product_id, "media": media})


def create_zipup(gql, db_products, db_variants, loc, apply: bool) -> None:
    print("\nZIPUP\n" + "-" * 5)
    p = db_products[ZIPUP_ID]
    dbvs = db_variants[ZIPUP_ID]

    existing = find_product(gql, p["name"]) or find_product(gql, p["name"].upper())
    if existing:
        print(f"  already on Shopify ({existing['id']}) — skipped")
        return

    images = [ROOT / rel for rel in json.loads(
        sqlite3.connect(f"file:{ROOT / 'wanas.db'}?mode=ro", uri=True)
        .execute("SELECT images FROM products WHERE product_id=?", (ZIPUP_ID,))
        .fetchone()[0]
    )]
    missing = [i for i in images if not i.exists()]

    print(f"  create {p['name']!r} · {p['category']}")
    print(f"  options: Size {p['sizes']} · Color {p['colors']}")
    print(f"  variants: {len(dbvs)} · price {p['price']} was {p['original_price']}")
    print(f"  images: {len(images)}" + (f" ({len(missing)} missing on disk)" if missing else ""))
    for m in missing:
        print(f"    ! not found: {m}")
    if not apply:
        print("  (dry run)")
        return

    created = gql(
        PRODUCT_CREATE,
        {
            "product": {
                "title": p["name"],
                "descriptionHtml": p["description"] or "",
                "productType": p["category"],
                "vendor": "Wanas Gallery",
                "status": "ACTIVE",
                "productOptions": [
                    {"name": "Size", "values": [{"name": s} for s in p["sizes"]]},
                    {"name": "Color", "values": [{"name": c} for c in p["colors"]]},
                ],
            }
        },
    )["productCreate"]["product"]
    pid = created["id"]
    print(f"  created {pid}")

    variants = [
        {
            "optionValues": [
                {"optionName": "Size", "name": v["size"]},
                {"optionName": "Color", "name": v["color"]},
            ],
            "price": money(v["price"]),
            **({"compareAtPrice": money(v["original_price"])} if v["on_sale"] else {}),
            "inventoryItem": {"tracked": True},
        }
        for v in dbvs
    ]
    made = gql(VARIANTS_BULK_CREATE, {"productId": pid, "variants": variants})[
        "productVariantsBulkCreate"
    ]["productVariants"]
    print(f"  {len(made)} variant(s) created")

    by_key = {(v["size"], v["color"]): v for v in dbvs}
    quantities = []
    for sv in made:
        opts = {o["name"].lower(): o["value"] for o in sv["selectedOptions"]}
        dbv = by_key.get((opts.get("size"), opts.get("color")))
        if dbv:
            quantities.append(
                {
                    "inventoryItemId": sv["inventoryItem"]["id"],
                    "locationId": loc,
                    "quantity": dbv["stock_qty"],
                }
            )
    if quantities:
        gql(
            INVENTORY_SET,
            {
                "input": {
                    "name": "available",
                    "reason": "correction",
                    "ignoreCompareQuantity": True,
                    "quantities": quantities,
                }
            },
        )
        print(f"  stock set on {len(quantities)} variant(s)")

    present = [i for i in images if i.exists()]
    if present:
        print(f"  uploading {len(present)} image(s)...")
        upload_images(gql, pid, present)


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    env = load_env(ROOT / ".env")
    gql = Shopify(
        env["SHOPIFY_STORE_DOMAIN"],
        env["SHOPIFY_ADMIN_TOKEN"],
        env.get("SHOPIFY_API_VERSION", "2025-01"),
    )
    shop = gql("{shop{name currencyCode}}")["shop"]
    print(f"Connected to {shop['name']} ({shop['currencyCode']})")

    loc, err = probe_location(gql)
    if err:
        sys.exit("\n" + err + "\n\nNothing has been written.")

    db_products, db_variants = read_db()

    fix_worker_jacket(gql, db_variants, loc, args.apply)
    create_zipup(gql, db_products, db_variants, loc, args.apply)

    if not args.apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply.")
    else:
        print("\nDone. Re-run scripts/shopify_sync.py to confirm the catalog matches.")


if __name__ == "__main__":
    main()
