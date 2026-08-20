# CLAUDE.md

Instructions and context for Claude Code sessions working in this repository.

## Project Purpose

A Shopify-connected e-commerce chatbot (WhatsApp) for **Wanas Gallery**, an
Egyptian streetwear brand. The product is the chatbot, its backend, and the
Shopify integration — not a general-purpose website or admin platform.

## Architecture

```
Shopify    -> product/store source of truth (orders, live inventory, live price)
              and, via webhooks, the trigger for order-status pushes
FastAPI    -> backend/API, app.py is the composition root (uvicorn app:app)
Chatbot    -> agent/runtime/tools/LLM (chatbot/)
Gemini     -> current LLM provider, behind a provider abstraction; also reads
              voice notes and photos
PostgreSQL -> chat/session history, carts, shipping rates, staff,
              human-handoff queue, plus catalog metadata Shopify can't hold
              (style, department, collection, size charts, per-colour photos)
Railway    -> production hosting
```

A fuller picture, including the five decisions the design follows from, is in
`docs/ARCHITECTURE.md`.

**Inbound messages are not answered in the request.** The webhook verifies,
claims the message id, downloads media and returns 200; the agent turn runs on
a worker thread after a short debounce window (`chatbot/dispatcher.py`). Never
move work back into the endpoint: it is `async`, the turn is synchronous, and
one message would block every other conversation in the process.

When the bot sells something it creates a real Shopify order
(`orderCreate`); Shopify decrements inventory itself. Price and stock are
read live from Shopify per message, matched to the local catalog by SKU; if
Shopify is unreachable the bot falls back to the local database's numbers
and logs a warning once, rather than failing the conversation.

## Important Files/Directories

```
app.py                  composition root
AGENTS.md                business rules and data assumptions (variant math,
                          per-colour pricing, shipping/order rules, session
                          limits) — read this before touching chatbot/backend
                          business logic
backend/
  config.py               env-driven settings, no hardcoded credentials
  db.py                   SQLAlchemy engine/session
  models.py                ORM models (Client, Product, Variant, Order, ...,
                            Staff, StaffQueueItem, ShippingRate)
  cli.py                   python -m backend.cli <seed|set-fee|create-staff|...>
  seed/                    seed importers (products, governorates)
  services/                 Order/Inventory/Notification/Catalog services,
                            shopify_catalog.py, shopify_inventory.py,
                            shopify_orders.py (Shopify read/write paths)
  integrations/
    shopify_client.py        GraphQL client for the live Shopify path
    whatsapp_client.py        Meta Cloud API client
  webhooks/shopify.py       inbound from Shopify: fulfilments and cancellations
                              -> order status -> the customer's tracking message
  services/search_terms.py  Arabic + franco vocabulary for the catalog search
chatbot/
  runtime.py                entry point: handle_message(channel, external_id, text)
  dispatcher.py             debounce + worker threads; what keeps the webhook fast
  agent.py                  the tool-use loop
  media.py                  voice notes and photos (see docs/MEDIA.md)
  interactive.py            tappable pickers, in a channel-neutral shape
  session.py                 DB-backed session storage
  display.py                 stored history -> bubbles a person can read;
                              shared by the harness and the dashboard
  media_serving.py           the local-file path guard both the harness and the
                              dashboard serve through
  providers/                  gemini.py (real), fake.py (tests/rehearsal), base.py
  tools/                      cart_tools, catalog_tools, order_tools, support_tools
  channels/whatsapp.py        the WhatsApp webhook + outbound sender registration
  harness/                    local dev-only chat UI (web + terminal), unauthenticated
                              by design and OFF unless HARNESS_ENABLED=1
dashboard/                 staff dashboard, its own top-level package:
                            conversations, Shopify (products/
                            orders/customers), statistics, the review queue,
                            and feature-flag settings; staff-login
                            authenticated, ON by default
                            (DASHBOARD_SESSION_SECRET gates login).
                            web.py (auth + conversations) stays one file;
                            shopify_api.py / stats_api.py / queue_api.py /
                            settings_api.py / customers_api.py are sibling
                            routers under the same guard, not a growing
                            single file -- see web.py's own docstring. To
                            show conversations it reads chatbot/session.py,
                            chatbot/display.py, chatbot/messages.py, and
                            chatbot/media_serving.py directly -- the same
                            read-only surface the harness reads through --
                            plus backend/ services for everything else;
                            chatbot/ never imports back from dashboard/.
docs/                    ARCHITECTURE.md, OPERATIONS.md, MEDIA.md
data/                    products_seed.json, size_charts.json, governorates.json,
                          merge_catalog.py, images/, size-charts/ — catalog
                          metadata Shopify has no field for, NOT a duplicate
                          product database (price/stock come from Shopify)
scripts/                 shopify_sync.py (ongoing catalog/stock reconciliation),
                          shopify_set_skus.py (link local variant_id -> Shopify
                          SKU), shopify_check_live.py (read-only smoke check),
                          migrate_add_shopify_order_columns.py (one-time schema
                          upgrade for pre-existing databases) — all dry-run by
                          default, idempotent, need --apply
tests/                   pytest suite, see README.md
```

## Database

- PostgreSQL is the production database (`DATABASE_URL=postgresql+psycopg://...`).
  SQLite (`sqlite:///./wanas.db`) is fine for local development only.
- Chat/session history is persisted (`sessions` table / `chatbot/session.py`)
  — this is a required production feature. **Never** remove it or replace it
  with an in-memory store.
- No Alembic — tables are created at startup via `Base.metadata.create_all`
  (`app.py`), which adds missing tables but not missing columns on existing
  ones; see `scripts/migrate_add_shopify_order_columns.py` for that case.
  Seed a new database with `python -m backend.cli seed`.
- Important models: `Client`, `Product`, `Variant`, `Order`/`OrderItem`
  (carry Shopify order id/number columns), `ShippingRate`, `Staff`,
  `StaffQueueItem` (human-handoff / item-swap / alert queues),
  `WebhookEvent` (idempotency).

## Shopify

- Source of truth for **orders, live inventory, live price**. Do not build a
  second product database in Postgres for these fields.
- Postgres still legitimately holds catalog data Shopify has no field for
  (`style`, `department`, `collection`, size charts) — see
  `backend/services/catalog.py` (the overlay: Shopify numbers over local
  rows). `variant_id` <-> Shopify variant is matched by SKU.
- Product photos follow the same overlay as price/stock, not the
  style/department list above: `shopify_catalog.LiveVariant.image_url` (read
  alongside price/stock, same call) wins over the local `data/images/` file
  for that colour when staff have set one in Shopify Admin —
  `catalog._overlay_images` never invents a new colour split the local
  gallery does not already have. `WhatsAppClient.send_image` sends an
  `http(s)` path by `link`; the local-file upload/cache path
  (`media_id_for`) is what still serves the twelve size charts and any
  product colour Shopify has no photo for yet. See `docs/ARCHITECTURE.md`.
- Manual inventory decrement is deliberately **not** in the order path —
  Shopify already decrements on `orderCreate`; doing it twice would silently
  oversell.
- Order **status** comes back the other way: `backend/webhooks/shopify.py`
  turns `orders/fulfilled` / `orders/cancelled` / `fulfillments/update` into
  `orders.advance_status`, which is what makes the tracking messages fire.
  Statuses move forward one stage at a time — never assign `order.status`
  directly.
- `SHOPIFY_ADMIN_TOKEN` and `SHOPIFY_WEBHOOK_SECRET` live only in `.env`.
  Never print, log, or commit either.

## LLM

- Provider: **Gemini**, via `chatbot/providers/gemini.py`, behind
  `chatbot/providers/base.py`. Nothing above `chatbot/providers/` may import
  a vendor SDK — Gemini is called over raw HTTPS, so swapping providers is
  one new class + one config value (`LLM_PROVIDER`).
- Config: `LLM_PROVIDER`, `LLM_API_KEY` (or `GEMINI_API_KEY`), `LLM_MODEL`
  (or `GEMINI_MODEL`, blank lets the provider pick a working model).
- `LLM_PROVIDER=fake` runs the scripted provider used by tests and by the
  harness when no key is set.
- The provider also owns media: `transcribe()` for voice notes and
  `inspect_image()` for photos, both declared by `supports_audio` /
  `supports_vision` so the runtime can fall back to a human *before* spending a
  call finding out. A vision reading is a hint about which tool to call, never
  a fact — see `docs/MEDIA.md`.

## Development

```bash
make dev                  # or: pip install -r requirements-dev.txt
cp .env.example .env
make seed
make harness              # or: python -m chatbot.harness (terminal)
make run
```

`make help` lists the rest.

## Testing

```bash
make check                                          # ruff + the full suite (what CI runs)
make lint                                           # ruff check ., no autofix
pytest tests/test_order_transaction.py              # one file
pytest tests/test_order_transaction.py::test_name   # one test
```

Every test gets a fresh schema and an in-memory fake Shopify shelf
(`tests/conftest.py`), and the suite blanks real `LLM_API_KEY` /
`SHOPIFY_*` / `WHATSAPP_*` env vars before importing the app, so it never
reads a developer's `.env` by accident. Two markers opt out of the default:

- `@pytest.mark.no_shopify` — skips installing the fake Shopify shelf, to
  exercise the "Shopify unreachable" fallback path.
- `@pytest.mark.live` (`tests/test_conversation_live.py`) — hits a real model
  and costs quota; skipped unless run explicitly:
  `RUN_LIVE_TESTS=1 pytest tests/test_conversation_live.py -v`.

Run the suite against PostgreSQL before deploying — `tests/test_order_transaction.py`
(the atomic-order concurrency test) is the one that depends on it most:
`DATABASE_URL=postgresql+psycopg://user:pass@localhost/wanas make test`.

Dependencies are **pinned** in `requirements.txt`. Railway rebuilds from it on
every deploy, so an unpinned range means the build that ships is not the build
that was tested. Bump deliberately, with the suite green.

## Production

Railway startup command — **do not change this**:

```
uvicorn app:app
```

`DATABASE_URL` must point at PostgreSQL in production. `HARNESS_ENABLED` and
`CHATBOT_DEBUG` both default to off and must stay off. The full pre-launch
checklist — including `SHOPIFY_WEBHOOK_SECRET`, whose absence silently means no
tracking message ever fires — is in `docs/OPERATIONS.md`.

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
  by anyone other than a developer — it is unauthenticated by design, and it
  now ships off precisely so that forgetting a variable is not what exposes it.
- Never accept a webhook without verifying its signature. Meta signs with hex
  HMAC-SHA256, Shopify with base64; with no secret configured the Shopify
  endpoint refuses everything, and that is the correct behaviour.
- Never expose `DASHBOARD_SESSION_SECRET`, and never weaken
  `dashboard/web.py`'s login so it signs a session without one —
  same shape as the Shopify webhook rule above: no secret means refuse, not
  fall back to something guessable.

## Legacy Architecture

The old custom e-commerce website (a React storefront) and the old
server-rendered admin UI reading the local database were **removed from this
repository**. The storefront moved to a Shopify theme; Shopify Admin is now
where products, inventory, and orders are managed by staff. Do not recreate
the old website architecture unless explicitly requested.

The one gap that removal left — `request_human` pausing a conversation with
no UI to resolve it — is closed. `dashboard/` is a new, purpose-built
staff dashboard: it lists conversations, shows one in full, and lets a
logged-in staff member reply to a *paused* one or resolve it without a
reply. See "The staff dashboard" in `docs/ARCHITECTURE.md`, and
`DASHBOARD_SESSION_SECRET` / `python -m backend.cli create-staff` in
`docs/OPERATIONS.md`.

**This is no longer read-only for Shopify.** The dashboard grew into the
single control/monitoring surface for the whole business: alongside
conversations it has a Shopify section (products — view, create, edit;
orders — view, fulfil, cancel, edit across *both* the bot and the website;
customers), store-wide statistics (KPIs, charts, built from a live Shopify
read since only bot orders ever reach Postgres — see
`backend/services/dashboard_stats.py`), the `item_swap`/`alert` review
queue, and staff-toggleable feature flags
(`backend/services/runtime_flags.py`). Shopify is still the source of truth
for price/stock/orders; product create/edit pushes to Shopify first and
mirrors the wanas.db-only fields (`category`/`department`/`style`/
`collection`/`size_chart`) after — see the module docstring on
`backend/services/shopify_admin_products.py` for exactly where that line
is drawn (no file-upload images yet, no refunds — this shop is
cash-on-delivery with nothing to refund against, and removing a variant
from an existing Shopify product is deliberately left to Shopify Admin).
