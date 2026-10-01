# Wanas → Rehla cleanup plan (audit only, nothing changed yet)

Scope: `S:\E-commerce\Rehla` @ `4980bf2` (main = rehla). 189 of 534 tracked files
mention `wanas` / `ونس` / `WNS`. By area (hits): tests 754, dashboard 129, docs 75,
integrations 74, scripts 64, assistant 44, data 44, domain 38, theme 19, common 10,
app.py 7, config 3. No tracked file is *named* wanas (only the `data/images`,
`size-charts/wns-boxy-tee.png` family, see 2c). No `restore_wanas` file exists; the only
checkpoint reference is `REHLA.md:5` (`S:\E-commerce\_checkpoint\RESTORE.md`).

## 1. Safe to change (no production effect)

| What | Where | Action |
|---|---|---|
| Brand prose | `CLAUDE.md`, `AGENTS.md`, `README.md`, `REHLA.md` | rewrite for Rehla (women's clothing, local store, RHL- refs); drop checkpoint line |
| `CHANGELOG.md` (20) | history | leave history as is; add one entry for the rename |
| Docs | `docs/ARCHITECTURE.md`, `OPERATIONS.md`, `WHATSAPP_TEMPLATES.md` (prose only), `eval_order_flow_report.md` | text edits; the eval report is a stale Wanas artefact → delete |
| Comments / docstrings | `app.py`, `config/settings.py`, `domain/**`, `integrations/**`, `assistant/**`, `dashboard/*.py` | `wanas.db` → `rehla.db` in prose only |
| `.env.example`, `Makefile` (`rm -f wanas.db…`), `pyproject.toml`, `.github/workflows/ci.yml` (`POSTGRES_DB: wanas`) | config | rename to rehla (CI DB is throwaway) |
| `theme/size-chart.liquid` (`.wns-*` CSS classes) | pasted by hand into a theme | rename together; theme is not loaded by the app |
| `data/merge_catalog.py`, `scripts/eval_order_flow.py`, `scripts/shopify_sync.py` | Wanas-only catalog tooling | delete if unused by Rehla (Rehla builds catalog via `scripts/rehla/build_seed.py`); confirm with grep |
| Tests built on Wanas products (`wanas-hoodie`, `boxy-wns-tee`, `WNS-1001`…): ~754 hits in ~60 files | tests | see §4 |
| Brand-word allowlist `assistant/showcase.py:127`, `reply_rules.py:85-114,610` (Wanas/WNS/ونس normalisation) | code | keep `rehla`, drop `wanas`/`wns` **after** the tests move; keep `WNS-` accepted in `reply_facts.py:67` / `agent.py:927` until old orders age out (cheap, harmless) |

## 2. Needs care (could break production) — and how to do each safely

a. **Env var `WANAS_TEST_DATABASE_URL`** (24 hits: conftest, CI, README, CLAUDE.md). Test-only,
   Railway never reads it. Rename to `REHLA_TEST_DATABASE_URL`; `conftest.py` reads the new
   name, falls back to the old one. Safe.
   (No other env var contains WANAS/WNS. `WANAS_SCRAPE` x2 is in a script only.)

b. **Cookie `wanas_staff`** (`dashboard/web.py:79` + the `Cookie(default=None)` parameter name in 12
   dashboard modules). The cookie name = FastAPI parameter name. Renaming logs every staff
   member out once. Change to `rehla_staff` and also accept `wanas_staff` as fallback for one
   release so no one is logged out; then remove. Medium risk, mechanical.

c. **SKUs / product ids / image paths** (`wanas-hoodie…`, `boxy-wns-tee`, `data/images/wanas-*`).
   Stored in DB rows (`variants.variant_id`, cart lines, order items) and, on Shopify, as
   SKUs. In Rehla's own seed (`data/rehla_source` → `products_seed.json`) these are already
   `rehla-*`. Verify no Wanas product rows remain in the seed; **do not** rewrite ids in an
   existing DB. Remaining uses are in tests / Wanas tooling only (§1, §4).

d. **DB names**: `sqlite:///./wanas.db` default in `config/settings.py:495`. Changing the
   default to `rehla.db` is safe *only* because production sets `DATABASE_URL` (Postgres)
   explicitly; confirm on Railway that the var is set before deploy. Table/column names do not
   contain wanas, so no migration. Local `.env` already points to `sqlite:///./rehla.db`.
   The Postgres DB *name* on Railway is Railway's and untouched.

e. **WhatsApp template names** `wanas_order_update`, `wanas_feedback_request`,
   `wanas_order_confirmation`, `wanas_back_in_stock`, `wanas_abandoned_cart`
   (docs only; the code reads `WHATSAPP_TEMPLATE_*` env vars). They are approved at Meta by
   name. **Do not rename in code.** Renaming = creating new templates at Meta + approval, then
   switching the env values on Railway. Out of scope (you said don't touch Meta); list as manual.

f. **Logger names `wanas.*`** (`wanas.runtime`, `wanas.session`, `wanas.latency`,
   `wanas.channel.*`, `wanas.alert_email`…) and contextvar `wanas_turn_telemetry`. Any Railway
   log filter / alert on the string `wanas.latency` stops matching. Rename to `rehla.*` once
   you confirm you have no saved log queries; otherwise keep.

g. **Browser storage key `wanas.lang`** (dashboard html). Rename = every staff member's
   language preference resets once. Read new key, fall back to old. Low risk.

h. **Shopify `note=wanas-bot://order/…`** (`integrations/shopify/inventory.py:231,254`). Written
   into inventory adjustment notes; only cosmetic. Change to `rehla-bot://`. (Shopify is not
   touched by us, but this is a string the app sends; since Rehla runs without Shopify per
   `REHLA.md`, it is dead code today.)

i. **`SHOPIFY_VENDOR=Wanas Gallery`** in `.env.example`: placeholder; set to Rehla. The real
   value lives in Railway/.env — check it there, not in code.

j. **Seed fix `RETIRED_SIZE_CHARTS`** (wns-boxy-tee) in `domain/seed/products.py`: Wanas-only
   chart ids; remove with the Wanas products, and the `data/size-charts/wns-*.png` files.

## 3. GitHub + Railway rename (`wanas` → `rehla`)

- GitHub: Settings → Rename `mklabs-ecommerce/wanas` → `rehla`. Old URL redirects (web and
  git), so existing clones keep working; update `origin` anyway.
- Railway: its GitHub link is keyed on the repository **id**, not the name, so the service
  normally keeps deploying after a rename. I cannot prove it from here (Railway CLI is not
  logged in). Verification step after rename: push a trivial commit to `main` and confirm a
  deploy triggers; if not, Service → Settings → Source → Disconnect/Reconnect to `rehla`.
  Also: Railway service rename is cosmetic (Settings → name); do it after, no URL change
  unless the generated domain is derived from the name (check before renaming).
- **Caveat:** `mklabs-ecommerce/wanas` is where `S:\E-commerce\wanas` *used to* point and the
  old Wanas history/tags (`wanas-v1`, PRs) live there. You said the archive is
  `wanas-archive`; that repo was not reachable (private/not found) in phase 1. Rename should
  happen only once the archive repo exists and holds the Wanas history, otherwise two repos
  would claim the same history.
- The rename also keeps the `wanas-v1` tag; it is harmless but I'd keep it as the pointer to
  where Wanas diverged.

## 4. Tests

Baseline `make check` has **not** been run yet (needs a venv); I'll run it first in phase 3 so
we know which failures are pre-existing. Plan: the generic fixtures use the Wanas product
set. Migrate them to a small Rehla fixture set (a top, a hoodie-less category such as
`rehla-black-t-shirt`, one with sleeves variants) rather than edit 754 lines by hand:
1. Shared fixtures in `conftest.py` / seed helper for Rehla products.
2. Tests asserting Wanas-specific catalogue behaviour (hoodies, zip, quarter-zip, WNS tee
   size charts, `test_size_chart_*`, `test_sleeves`) → port where the behaviour still exists
   for Rehla, delete where it only existed for Wanas garments.
3. Tests that merely use a product name as sample data → mechanical rename.
Each batch: `pytest` green before the next.

## 5. Phase 3 order of work

1. Baseline `make check` (venv, pytest). 2. Docs/CLAUDE.md/AGENTS.md/README/REHLA.md.
3. Env/Makefile/CI/.env.example with fallbacks (2a, 2d). 4. Cookie + lang key with fallbacks
(2b, 2g). 5. Log names (2f) only if you OK it. 6. Tests migration. 7. Delete Wanas-only
tooling and the stale report. 8. `make check` green, commit, push main, Railway deploy,
`/health` + one WhatsApp message. 9. GitHub rename + remote update + deploy check.

## Manual leftovers for you
- Meta templates `wanas_*` (§2e) — decide whether to recreate under `rehla_*`.
- Railway env: nothing needs changing for code compatibility; later set `REHLA_TEST…` is not
  needed there. Check `DATABASE_URL` is set explicitly.
- Rotate the GitHub PAT and Railway token that were pasted in chat.
- Railway CLI/token login so I can deploy and verify.
