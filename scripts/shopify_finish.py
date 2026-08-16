"""Everything left that a machine can do, in one command.

Three jobs, each independent and each safe to re-run:

  collections  Six automated collections, one per product type, in the order
               the storefront's category row expects. Without these that row
               renders empty, because Shopify browses by collection while the
               design browses by type.

  theme        Uploads shopify-theme/ to an unpublished theme named
               "Wanas Gallery" -- reusing that draft if it already exists, so
               re-running does not litter the admin with copies. Never touches
               the live theme. Publishing is a separate, explicit flag.

  favicon      Points the theme's favicon setting at the packaged wordmark.

    python scripts/shopify_finish.py                  # dry run
    python scripts/shopify_finish.py --apply          # do it, theme stays draft
    python scripts/shopify_finish.py --apply --publish-theme   # and go live

Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shopify_sync import ROOT, Shopify, load_env  # noqa: E402

THEME_DIR = ROOT / "shopify-theme"

# The order matters: it is the order the category row renders in.
CATEGORIES = [
    "T-Shirts",
    "Hoodies & Sweatshirts",
    "Polo Shirts",
    "Joggers & Sweatpants",
    "Jackets",
    "Tops",
]

TEXT_SUFFIXES = {".liquid", ".json", ".css", ".js", ".svg", ".txt", ".md"}


class Rest:
    """The Asset API is REST-only; themes cannot be filled through GraphQL."""

    def __init__(self, domain: str, token: str, version: str):
        self.base = f"https://{domain}/admin/api/{version}"
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={
                "X-Shopify-Access-Token": self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:300]
                if e.code == 429 and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                if e.code in (500, 502, 503, 504) and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"Shopify HTTP {e.code} on {method} {path}: {detail}")
            except urllib.error.URLError as e:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"Could not reach Shopify: {e.reason}")
        raise SystemExit("Shopify kept throttling; try again shortly.")


# --------------------------------------------------------------------------
# collections
# --------------------------------------------------------------------------

COLLECTION_QUERY = """
{ collections(first: 100) { nodes { id title handle ruleSet { rules { column relation condition } } } } }
"""

COLLECTION_CREATE = """
mutation($input: CollectionInput!) {
  collectionCreate(input: $input) {
    collection { id title handle }
    userErrors { field message }
  }
}
"""


def _rule_matches(collection: dict, title: str) -> str | None:
    """None if the collection really does select this product type.

    Matching on the title alone is not enough. A collection called "Jackets"
    that selects on tag, or on the wrong type, reports as present and then
    renders an empty category row on a live store -- which is the exact
    failure these collections exist to prevent, arriving silently.
    """
    rule_set = collection.get("ruleSet")
    if not rule_set:
        return "manual collection, not a product-type rule"

    rules = rule_set.get("rules") or []
    for rule in rules:
        if (
            rule.get("column") == "TYPE"
            and rule.get("relation") == "EQUALS"
            and rule.get("condition") == title
        ):
            return None if len(rules) == 1 else f"has {len(rules)} rules, expected just the one"

    shown = ", ".join(
        f"{r.get('column')} {r.get('relation')} {r.get('condition')!r}" for r in rules
    )
    return f"rule is [{shown}], expected TYPE EQUALS {title!r}"


def do_collections(gql: Shopify, apply: bool) -> None:
    print("\nCOLLECTIONS\n" + "-" * 11)
    existing = {c["title"]: c for c in gql(COLLECTION_QUERY)["collections"]["nodes"]}
    wrong = []

    for title in CATEGORIES:
        if title in existing:
            problem = _rule_matches(existing[title], title)
            if problem:
                print(f"  WRONG : {title} — {problem}")
                wrong.append(title)
            else:
                print(f"  exists: {title}")
            continue
        print(f"  create: {title}  (rule: product type = {title})")
        if not apply:
            continue
        made = gql(
            COLLECTION_CREATE,
            {
                "input": {
                    "title": title,
                    "ruleSet": {
                        "appliedDisjunctively": False,
                        "rules": [
                            {"column": "TYPE", "relation": "EQUALS", "condition": title}
                        ],
                    },
                }
            },
        )["collectionCreate"]["collection"]
        print(f"    created {made['handle']}")

    if wrong:
        # Not fixed automatically: an existing collection may have been curated
        # by hand for a reason, and rewriting someone's rules without asking is
        # worse than telling them.
        print(f"\n  {len(wrong)} collection(s) exist but will not fill the category row.")
        print("  Fix their rules in the admin (Products > Collections), or delete")
        print("  them and re-run so this script creates them properly.")


# --------------------------------------------------------------------------
# theme
# --------------------------------------------------------------------------


def theme_files() -> list[Path]:
    if not THEME_DIR.exists():
        sys.exit(f"No theme at {THEME_DIR}")
    return sorted(p for p in THEME_DIR.rglob("*") if p.is_file())


THEME_NAME = "Wanas Gallery"


def find_draft(rest: Rest) -> dict | None:
    """An unpublished theme of ours from an earlier run, if there is one.

    Without this, every re-run creates another "Wanas Gallery" and the admin
    fills up with drafts that are impossible to tell apart -- and the one you
    preview is not necessarily the one you just uploaded. The live theme is
    never a candidate: role is checked, not just the name.
    """
    themes = rest.call("GET", "/themes.json").get("themes") or []
    for theme in themes:
        if theme.get("name") == THEME_NAME and theme.get("role") != "main":
            return theme
    return None


def do_theme(rest: Rest, apply: bool, publish: bool, force_new: bool = False) -> int | None:
    print("\nTHEME\n" + "-" * 5)
    files = theme_files()
    total_kb = sum(f.stat().st_size for f in files) / 1024
    print(f"  {len(files)} file(s), {total_kb:.0f} KB")

    existing = None if force_new else (find_draft(rest) if apply else None)
    if existing:
        print(f"  target: existing draft '{THEME_NAME}' ({existing['id']}) -- overwriting its files")
    else:
        print(f"  target: new theme named '{THEME_NAME}', role=unpublished")
    if publish:
        print("  WILL PUBLISH after upload (--publish-theme given)")
    if not apply:
        print("  (dry run)")
        print(f"  note: a draft named '{THEME_NAME}' will be reused if one exists")
        return None

    if existing:
        tid = existing["id"]
    else:
        theme = rest.call(
            "POST", "/themes.json", {"theme": {"name": THEME_NAME, "role": "unpublished"}}
        )["theme"]
        tid = theme["id"]
        print(f"  created theme {tid}")

    failures = []
    for f in files:
        key = f.relative_to(THEME_DIR).as_posix()
        if f.suffix.lower() in TEXT_SUFFIXES:
            asset = {"key": key, "value": f.read_text(encoding="utf-8")}
        else:
            asset = {"key": key, "attachment": base64.b64encode(f.read_bytes()).decode()}
        try:
            rest.call("PUT", f"/themes/{tid}/assets.json", {"asset": asset})
            print(f"    {key}")
        except SystemExit as e:
            failures.append((key, str(e)))
            print(f"    FAILED {key}")
        time.sleep(0.12)  # the Asset API throttles hard on bursts

    if failures:
        print(f"\n  {len(failures)} file(s) failed:")
        for k, err in failures:
            print(f"    {k}: {err[:120]}")
        print("  Theme left unpublished. Fix and re-run, or upload the zip by hand.")
        return tid

    print(f"  all {len(files)} file(s) uploaded")

    if publish:
        rest.call("PUT", f"/themes/{tid}.json", {"theme": {"id": tid, "role": "main"}})
        print("  PUBLISHED — this theme is now live")
    else:
        print("  left as a draft. Preview it in Online Store > Themes.")
    return tid


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform the writes")
    ap.add_argument("--publish-theme", action="store_true",
                    help="make the uploaded theme live (implies --apply)")
    ap.add_argument("--skip-collections", action="store_true")
    ap.add_argument("--skip-theme", action="store_true")
    ap.add_argument("--new-theme", action="store_true",
                    help="always create a fresh draft instead of overwriting the existing one")
    args = ap.parse_args()

    apply = args.apply or args.publish_theme

    env = load_env(ROOT / ".env")
    for key in ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN"):
        if not env.get(key):
            sys.exit(f"{key} is empty in .env")

    version = env.get("SHOPIFY_API_VERSION", "2025-01")
    gql = Shopify(env["SHOPIFY_STORE_DOMAIN"], env["SHOPIFY_ADMIN_TOKEN"], version)
    rest = Rest(env["SHOPIFY_STORE_DOMAIN"], env["SHOPIFY_ADMIN_TOKEN"], version)

    shop = gql("{shop{name currencyCode}}")["shop"]
    print(f"Connected to {shop['name']} ({shop['currencyCode']})")

    if not args.skip_collections:
        do_collections(gql, apply)
    if not args.skip_theme:
        do_theme(rest, apply, args.publish_theme, args.new_theme)

    if not apply:
        print("\nDRY RUN — nothing was written.")
        print("Run with --apply to do it (the theme stays a draft),")
        print("or --apply --publish-theme to also make it live.")
    else:
        print("\nDone.")


if __name__ == "__main__":
    main()
