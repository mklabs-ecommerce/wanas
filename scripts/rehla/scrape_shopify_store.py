#!/usr/bin/env python3
"""Scrape a Shopify storefront's public JSON endpoints and info pages.

Usage:
    python scripts/scrape_shopify_store.py https://rehlaeg.online --out scraped/rehlaeg

Only uses `requests` and the standard library. Polite by default (1 req/sec,
retries with backoff on 429/5xx). Idempotent: re-running skips images that
already exist and simply re-fetches / overwrites JSON and page text (which are
cheap and should stay fresh).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = "RehlaCatalogScraper/1.0 (+https://rehlaeg.online; contact: abdelhamidhazem97@gmail.com)"
REQUEST_DELAY_SECONDS = 1.0
MAX_RETRIES = 5
TIMEOUT = 30

INFO_PAGE_CANDIDATES = [
    "/policies/shipping-policy",
    "/policies/refund-policy",
    "/policies/privacy-policy",
    "/policies/terms-of-service",
    "/policies/contact-information",
    "/pages/shipping",
    "/pages/shipping-policy",
    "/pages/returns",
    "/pages/return-policy",
    "/pages/refund-policy",
    "/pages/contact",
    "/pages/contact-us",
    "/pages/faq",
    "/pages/faqs",
    "/pages/about",
    "/pages/about-us",
    "/pages/size-guide",
    "/pages/size-chart",
    "/pages/sizing",
]


@dataclass
class ScrapeReport:
    total_products: int = 0
    total_variants: int = 0
    total_images_downloaded: int = 0
    total_images_skipped_existing: int = 0
    total_collections: int = 0
    pages_found: list = field(default_factory=list)
    pages_missing: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    products_missing_price: list = field(default_factory=list)
    products_missing_images: list = field(default_factory=list)
    products_missing_sku: list = field(default_factory=list)
    notes: list = field(default_factory=list)


class PoliteSession:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_time = 0.0

        self.robots = robotparser.RobotFileParser()
        self.robots.set_url(urljoin(self.base_url, "/robots.txt"))
        try:
            self.robots.read()
        except Exception:
            self.robots = None

    def allowed(self, path: str) -> bool:
        if self.robots is None:
            return True
        try:
            return self.robots.can_fetch(USER_AGENT, urljoin(self.base_url, path))
        except Exception:
            return True

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS - elapsed)

    def get(self, path_or_url: str, **kwargs) -> requests.Response | None:
        url = path_or_url if path_or_url.startswith("http") else urljoin(self.base_url + "/", path_or_url.lstrip("/"))
        path = urlparse(url).path or "/"
        if not self.allowed(path):
            return None

        backoff = 1.0
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            self._last_request_time = time.monotonic()
            try:
                resp = self.session.get(url, timeout=TIMEOUT, **kwargs)
            except requests.RequestException:
                if attempt == MAX_RETRIES:
                    return None
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES:
                    return resp
                time.sleep(backoff)
                backoff *= 2
                continue
            return resp
        return None


def strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_all_products(session: PoliteSession, report: ScrapeReport) -> list[dict]:
    products = []
    page = 1
    while True:
        resp = session.get(f"/products.json?limit=250&page={page}")
        if resp is None:
            report.failures.append(f"products.json page {page}: blocked by robots.txt or request failed")
            break
        if resp.status_code != 200:
            report.failures.append(f"products.json page {page}: HTTP {resp.status_code}")
            break
        data = resp.json()
        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
    return products


def fetch_collections(session: PoliteSession, report: ScrapeReport) -> list[dict]:
    collections = []
    page = 1
    while True:
        resp = session.get(f"/collections.json?limit=250&page={page}")
        if resp is None:
            report.failures.append(f"collections.json page {page}: blocked or failed")
            break
        if resp.status_code != 200:
            report.failures.append(f"collections.json page {page}: HTTP {resp.status_code}")
            break
        data = resp.json()
        batch = data.get("collections", [])
        if not batch:
            break
        collections.extend(batch)
        page += 1
    return collections


def fetch_collection_products(session: PoliteSession, handle: str, report: ScrapeReport) -> list[int]:
    product_ids = []
    page = 1
    while True:
        resp = session.get(f"/collections/{handle}/products.json?limit=250&page={page}")
        if resp is None or resp.status_code != 200:
            if resp is not None:
                report.failures.append(f"collections/{handle}/products.json page {page}: HTTP {resp.status_code}")
            break
        data = resp.json()
        batch = data.get("products", [])
        if not batch:
            break
        product_ids.extend(p["id"] for p in batch)
        page += 1
    return product_ids


def download_image(session: PoliteSession, url: str, dest: Path, report: ScrapeReport) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        report.total_images_skipped_existing += 1
        return True
    resp = session.get(url)
    if resp is None or resp.status_code != 200:
        report.failures.append(f"image download failed: {url}")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    report.total_images_downloaded += 1
    return True


def image_filename(index: int, src_url: str) -> str:
    path = urlparse(src_url).path
    ext = Path(path).suffix or ".jpg"
    return f"{index:02d}{ext}"


def variant_ids_for_image(image: dict) -> list[int]:
    return image.get("variant_ids", []) or []


def build_clean_product(product: dict, collections_by_product: dict[int, list[str]], images_root: Path, out_root: Path) -> dict:
    handle = product["handle"]
    image_dir = images_root / handle

    images_clean = []
    for idx, img in enumerate(product.get("images", []), start=1):
        fname = image_filename(idx, img["src"])
        rel_path = str((image_dir / fname).relative_to(out_root)).replace("\\", "/")
        variant_ids = variant_ids_for_image(img)
        images_clean.append({
            "id": img.get("id"),
            "position": img.get("position", idx),
            "src": img["src"],
            "local_path": rel_path,
            "variant_ids": variant_ids,
            "width": img.get("width"),
            "height": img.get("height"),
        })

    image_by_id = {img["id"]: img for img in images_clean}

    variants_clean = []
    for v in product.get("variants", []):
        variant_image = None
        if v.get("image_id") and v["image_id"] in image_by_id:
            variant_image = image_by_id[v["image_id"]]["local_path"]
        else:
            for img in images_clean:
                if v["id"] in img["variant_ids"]:
                    variant_image = img["local_path"]
                    break
        variants_clean.append({
            "id": v["id"],
            "title": v["title"],
            "option1": v.get("option1"),
            "option2": v.get("option2"),
            "option3": v.get("option3"),
            "sku": v.get("sku") or None,
            "price": v.get("price"),
            "compare_at_price": v.get("compare_at_price"),
            "available": v.get("available"),
            "grams": v.get("grams"),
            "barcode": v.get("barcode") or None,
            "image_local_path": variant_image,
        })

    body_html = product.get("body_html") or ""
    return {
        "id": product["id"],
        "handle": handle,
        "title": product["title"],
        "vendor": product.get("vendor"),
        "product_type": product.get("product_type"),
        "tags": product.get("tags", []),
        "collections": collections_by_product.get(product["id"], []),
        "options": [
            {"name": o["name"], "values": o.get("values", [])}
            for o in product.get("options", [])
        ],
        "body_html": body_html,
        "body_text": strip_html(body_html),
        "variants": variants_clean,
        "images": images_clean,
        "created_at": product.get("created_at"),
        "updated_at": product.get("updated_at"),
        "published_at": product.get("published_at"),
    }


def scrape_info_pages(session: PoliteSession, out_dir: Path, report: ScrapeReport):
    out_dir.mkdir(parents=True, exist_ok=True)
    seen_slugs = set()
    for path in INFO_PAGE_CANDIDATES:
        resp = session.get(path)
        if resp is None or resp.status_code != 200:
            report.pages_missing.append(path)
            continue
        slug = path.strip("/").replace("/", "_")
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        html_content = resp.text
        (out_dir / f"{slug}.html").write_text(html_content, encoding="utf-8")

        text = extract_main_text(html_content)
        (out_dir / f"{slug}.txt").write_text(text, encoding="utf-8")
        report.pages_found.append(path)


def extract_main_text(page_html: str) -> str:
    match = re.search(r"<main[^>]*>(.*?)</main>", page_html, flags=re.S | re.I)
    content = match.group(1) if match else page_html
    return strip_html(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store_url", help="e.g. https://rehlaeg.online")
    parser.add_argument("--out", default=None, help="output directory (default: scraped/<hostname-without-tld-suffix>)")
    args = parser.parse_args()

    store_url = args.store_url if args.store_url.startswith("http") else f"https://{args.store_url}"
    host = urlparse(store_url).netloc
    out_root = Path(args.out) if args.out else Path("scraped") / host.split(".")[0]
    out_root.mkdir(parents=True, exist_ok=True)
    images_root = out_root / "images"
    pages_root = out_root / "pages"

    session = PoliteSession(store_url)
    report = ScrapeReport()

    print(f"Scraping {store_url} -> {out_root}")

    print("Fetching collections...")
    collections = fetch_collections(session, report)
    report.total_collections = len(collections)
    (out_root / "collections.json").write_text(json.dumps(collections, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Mapping products to {len(collections)} collections...")
    collections_by_product: dict[int, list[str]] = {}
    for col in collections:
        handle = col["handle"]
        product_ids = fetch_collection_products(session, handle, report)
        for pid in product_ids:
            collections_by_product.setdefault(pid, []).append(handle)

    print("Fetching all products...")
    products_raw = fetch_all_products(session, report)
    report.total_products = len(products_raw)
    (out_root / "products_raw.json").write_text(json.dumps(products_raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Found {len(products_raw)} products.")

    print("Building clean product records and downloading images...")
    products_clean = []
    for product in products_raw:
        clean = build_clean_product(product, collections_by_product, images_root, out_root)
        report.total_variants += len(clean["variants"])

        if not any(v["price"] for v in clean["variants"]):
            report.products_missing_price.append(clean["handle"])
        if not clean["images"]:
            report.products_missing_images.append(clean["handle"])
        if not any(v["sku"] for v in clean["variants"]):
            report.products_missing_sku.append(clean["handle"])

        for img in clean["images"]:
            dest = out_root / img["local_path"]
            download_image(session, img["src"], dest, report)

        products_clean.append(clean)

    (out_root / "products_clean.json").write_text(json.dumps(products_clean, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Fetching info pages (policies, contact, FAQ, about, size guide)...")
    scrape_info_pages(session, pages_root, report)

    write_report(out_root, report)
    print(f"Done. Report written to {out_root / 'scrape_report.md'}")


def write_report(out_root: Path, report: ScrapeReport):
    lines = [
        "# Scrape Report",
        "",
        f"- Total products: {report.total_products}",
        f"- Total variants: {report.total_variants}",
        f"- Total collections: {report.total_collections}",
        f"- Images downloaded (new): {report.total_images_downloaded}",
        f"- Images skipped (already existed): {report.total_images_skipped_existing}",
        "",
        "## Info pages found",
    ]
    lines += [f"- {p}" for p in report.pages_found] or ["- (none)"]
    lines += ["", "## Info pages not found (tried, 404 or blocked)"]
    lines += [f"- {p}" for p in report.pages_missing] or ["- (none)"]

    lines += ["", "## Products missing price on all variants"]
    lines += [f"- {p}" for p in report.products_missing_price] or ["- (none)"]

    lines += ["", "## Products missing images"]
    lines += [f"- {p}" for p in report.products_missing_images] or ["- (none)"]

    lines += ["", "## Products missing SKU on all variants"]
    lines += [f"- {p}" for p in report.products_missing_sku] or ["- (none)"]

    lines += ["", "## Failures"]
    lines += [f"- {f}" for f in report.failures] or ["- (none)"]

    lines += ["", "## Notes"]
    lines += [f"- {n}" for n in report.notes] or ["- (none)"]

    (out_root / "scrape_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
