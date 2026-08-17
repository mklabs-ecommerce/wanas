# CLAUDE.md

Instructions and context for Claude Code sessions working in this repository.

## Project Purpose

This is a Shopify-connected e-commerce chatbot (WhatsApp) for **Wanas
Gallery**, an Egyptian streetwear brand. It is not a general-purpose website
or admin platform — the product is the chatbot, its backend, and the Shopify
integration.

## Current Architecture

```
Shopify   -> product/store source of truth (orders, live inventory, live price)
FastAPI   -> backend/API (app.py is the composition root)
Chatbot   -> agent/runtime/tools/LLM (chatbot/)
Gemini    -> current LLM provider, behind a provider abstraction
PostgreSQL -> application persistence: chat/session history, carts, shipping
              rates, staff, audit log, human-handoff queue, and catalog
              metadata Shopify can't hold (style, department, collection,
              size charts, per-colour photos)
Railway   -> production hosting
```

When the bot sells something it creates a real Shopify order (`orderCreate`);
Shopify decrements inventory itself. Price and stock are read live from
Shopify per message; if Shopify is unreachable the bot falls back to the
local database's numbers and logs a warning once, rather than failing the
conversation.

## Important Files/Directories

```
app.py                composition root — uvicorn app:app
AGENTS.md              business rules and data assumptions (variant math,
                        per-colour pricing, session/tool-loop limits,
                        the seventeen agent tools) — still accurate; read
                        its top "status note" for what's changed since
backend/
  config.py             env-driven settings, no hardcoded credentials
  db.py                 SQLAlchemy engine/session
  models.py              ORM models (Client, Product, Variant, Order, ...,
                          Staff, AuditLog, StaffQueueItem, ShippingRate)
  cli.py                 `python -m backend.cli <seed|set-fee|create-staff|...>`
  seed/                  seed importers (products, governorates)
  services/               Order/Inventory/Notification/Catalog services,
                          shopify_catalog.py, shopify_inventory.py,
                          shopify_orders.py (Shopify read/write paths)
  integrations/
    shopify_client.py      GraphQL client for the live Shopify path
    whatsapp_client.py      Meta Cloud API client
chatbot/
  runtime.py              entry point: handle_message(channel, external_id, text)
  agent.py                the tool-use loop
  session.py               DB-backed session storage
  providers/                gemini.py (real), fake.py (tests/rehearsal), base.py
  tools/                    cart_tools, catalog_tools, order_tools, support_tools
  channels/whatsapp.py      the WhatsApp webhook + outbound sender registration
  harness/                  local dev-only chat UI (web + terminal), unauthenticated
                            by design; HARNESS_ENABLED=0 removes it
data/                    products_seed.json, size_charts.json, governorates.json,
                          merge_catalog.py, images/, size-charts/ — catalog
                          metadata Shopify has no field for, NOT a duplicate
                          product database (price/stock come from Shopify)
scripts/                 shopify_sync.py, shopify_check_live.py,
                          shopify_set_skus.py, shopify_finish.py,
                          shopify_fix_structure.py,
                          migrate_add_shopify_order_columns.py
                          -- all dry-run by default, idempotent, need --apply
docs/components/         normative specs; 15 = tool contracts, 16 = supporting
                          tables. 03 (website) and 12 (admin dashboard) were
                          removed along with the code they described.
tests/                   pytest suite, see README.md for the breakdown
```

**Removed** (see "Legacy" below): `dashboard/`, `storefront/`, `web/`.

## Database

- PostgreSQL is the production database (`DATABASE_URL=postgresql+psycopg://...`).
  SQLite (`sqlite:///./wanas.db`) is fine for local development only — the
  schema depends on nothing SQLite-specific.
- Chat/session history is persisted (`sessions` table / session store in
  `chatbot/session.py`) — this is a required production feature. **Never**
  remove it or replace it with an in-memory store.
- No Alembic — tables are created at startup via `Base.metadata.create_all`
  (`app.py`). Seed once per new database: `python -m backend.cli seed`.
- Important models: `Client`, `Product`, `Variant`, `Order`/`OrderItem`
  (carry Shopify order id/number columns), `ShippingRate`, `Staff`,
  `AuditLog`, `StaffQueueItem` (human-handoff / item-swap / alert queues),
  `WebhookEvent` (idempotency).

## Shopify

- Shopify is the source of truth for **orders, live inventory, live price**.
  Do not build a second product database in Postgres for these fields.
- Postgres still legitimately holds catalog data Shopify has no field for:
  `style`, `department`, `collection`, size charts, per-colour images. This
  is intentional, not leftover duplication — see `backend/services/catalog.py`
  (the overlay: Shopify numbers over local rows) and `data/`.
  `variant_id` <-> Shopify variant is matched by SKU.
- Manual inventory decrement (`inventory.decrement`) is deliberately **not**
  in the order path — Shopify already decrements on `orderCreate`, and
  double-decrementing would silently oversell in the other direction.
- Credentials (`SHOPIFY_ADMIN_TOKEN`) live only in `.env`. Never print,
  log, or commit the token.

## LLM

- Provider: **Gemini**, via `chatbot/providers/gemini.py`, behind
  `chatbot/providers/base.py`. Nothing above `chatbot/providers/` may import
  a vendor SDK — the chatbot calls Gemini over raw HTTPS, not the Google SDK,
  so swapping providers is one new class + one config value
  (`LLM_PROVIDER`).
- Config: `LLM_PROVIDER`, `LLM_API_KEY` (or `GEMINI_API_KEY`), `LLM_MODEL`
  (or `GEMINI_MODEL`, blank lets the provider pick a working model),
  `LLM_DEBUG_PAYLOAD` (off by default — logs full request bodies, never the
  key).
- `LLM_PROVIDER=fake` runs the scripted provider used by tests and by the
  harness when no key is set.

## Development

```bash
pip install -r requirements.txt
cp .env.example .env
python -m backend.cli seed
python -m chatbot.harness.web    # or: python -m chatbot.harness (terminal)
uvicorn app:app --reload
```

Full walkthrough in `README.md`.

## Testing

```bash
python -m pytest tests/ -q
```

Opt-in live-model tests: `RUN_LIVE_TESTS=1 python -m pytest tests/test_conversation_live.py -v`.

## Production

Railway startup command — **do not change this**:

```
uvicorn app:app
```

(Railway typically runs it as `uvicorn app:app --host 0.0.0.0 --port $PORT`.)
`DATABASE_URL` must point at PostgreSQL in production. See
`PRODUCTION-DEPLOYMENT.md` for the full deploy walkthrough.

## Security Rules

- Never commit `.env`. `.env.example` holds variable names and safe
  placeholders only.
- Never expose API keys, the Gemini key, the Shopify Admin token, the
  WhatsApp access token/app secret, or any other credential — not in code,
  logs, commit messages, or chat output.
- Never replace production PostgreSQL with SQLite.
- Never remove chat/session persistence.
- Never modify production credentials or environment variables without
  explicit approval.
- Never revert the Railway startup command away from `uvicorn app:app`.
- Never re-enable the harness (`HARNESS_ENABLED=1`) in a deployment reachable
  by anyone other than a developer — it is unauthenticated by design.

## Legacy Architecture

The old custom e-commerce website (`web/` — a React storefront) and the
staff dashboard (`dashboard/` — server-rendered admin UI reading the local
database) were **removed from this repository**. The storefront moved to a
Shopify theme; Shopify Admin is now where products, inventory, and orders are
managed by staff. `storefront/` (the FastAPI cart/checkout API that backed
the old storefront) was removed with it.

Do not recreate the old website/dashboard architecture unless explicitly
requested. One known gap from this removal: `request_human` still pauses a
conversation and writes a handoff record, but there is currently no UI to
resolve one (previously the dashboard's job) — only the dev-only harness's
`/unpause` stand-in. This is a known limitation, not something to silently
"fix" by rebuilding the dashboard.
