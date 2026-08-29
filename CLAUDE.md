# CLAUDE.md

Instructions and context for Claude Code sessions working in this repository.

## Project Purpose

A Shopify-connected e-commerce chatbot for **Wanas Gallery**, an Egyptian
streetwear brand, on two first-class channels: **WhatsApp** (Meta Cloud API)
and **Instagram** (`"instagram_dm"`, DMs plus — shipped off by default —
public comments). The product is the chatbot, its API/backend, and the
Shopify integration — not a general-purpose website or admin platform.

## Architecture

```
Shopify    -> product/store source of truth (orders, live inventory, live price)
              and, via webhooks, the trigger for order-status pushes
FastAPI    -> the API, app.py is the composition root (uvicorn app:app)
Assistant  -> agent/runtime/tools/LLM (assistant/)
OpenRouter -> default LLM provider, behind a provider abstraction (Gemini and
              a scripted fake are the alternates); Whisper/a vision model
              handle voice notes and photos
PostgreSQL -> chat/session history, carts, shipping rates, staff,
              human-handoff queue, plus catalog metadata Shopify can't hold
              (style, department, collection, size charts, per-colour photos)
Railway    -> production hosting
```

A fuller picture, including the five decisions the design follows from, is in
`docs/ARCHITECTURE.md`.

**Inbound messages are not answered in the request.** The webhook verifies,
claims the message id, downloads media, **records the message**, and returns
200; the agent turn runs on a worker thread after a short debounce window
(`assistant/dispatcher.py`). Never move work back into the endpoint: it is
`async`, the turn is synchronous, and one message would block every other
conversation in the process.

**A conversation is visible on arrival, not on reply.** The ingest path calls
`assistant/runtime.py::record_inbound`, which writes the customer's message to
`sessions` in its *own committed transaction* before the debounce window opens.
The turn folds that provisional copy into the real message it stores
(`session.drop_provisional`, keyed on the platform message id). Never make the
dashboard depend on the bot having answered: a stuck, paused or crashing turn
used to be indistinguishable from a customer who never wrote, which is how
unanswered numbers went unnoticed. A provisional message left in the transcript
is not litter -- it is a message nobody ever answered, and the inbox's
`unanswered` filter is built on exactly that.

**And every message the shop sends is visible too.** A confirmation, a status
push, a feedback request, a back-in-stock notice, a cart nudge -- none of
these come out of an agent turn, so `domain/services/notifications.py` writes
them to `sessions` through a registered port
(`register_transcript_recorder` -> `assistant/runtime.py::record_outbound`,
wired in `app.py`; domain/ never imports assistant/). The line goes in
**inside the transaction that decides the message**, never from the
after-commit hook that sends it -- the committing connection holds SQLite's
write lock until that hook returns, which is why
`notifications.record_status_push` is separate from
`notifications.order_status_changed`. They are stored `by="system"`: a third
voice beside the model and staff, and one the `unanswered` filter skips.

When the bot sells something it creates a real Shopify order
(`orderCreate`); Shopify decrements inventory itself. Price and stock are
read live from Shopify per message, matched to the local catalog by SKU; if
Shopify is unreachable the bot falls back to the local database's numbers
and logs a warning once, rather than failing the conversation.

## Important Files/Directories

Layered by responsibility, with the vendor-integration layer organized by
external system:

```
app.py                  composition root (uvicorn app:app)
manage.py                ops CLI: python manage.py <seed|set-fee|create-staff|...>
AGENTS.md                business rules and data assumptions (variant math,
                          per-colour pricing, shipping/order rules, session
                          limits) — read this before touching domain/assistant
                          business logic
config/
  settings.py              env-driven settings, no hardcoded credentials --
                            imported by every layer below
common/                  zero-dependency shared kernel (no imports from any
                          layer below); safe for domain, integrations,
                          assistant, dashboard, and api/ to all import
  money.py                 Decimal helpers
  events.py                post-commit hooks (an outbound message must never
                            describe a write that later rolled back)
  security.py              shared Meta webhook-signature check (WhatsApp +
                            Instagram; Shopify signs differently, see
                            integrations/shopify/webhooks.py)
  servable_paths.py        the local-file path guard api/public_media.py and
                            assistant/media_serving.py both need
domain/                  persistence + business rules; no vendor HTTP calls,
                          no FastAPI routes; must never import assistant/
  db.py                    SQLAlchemy engine/session
  models.py                ORM models (Client, Product, Variant, Order, ...,
                            Staff, StaffQueueItem, ShippingRate)
  legal.py                 privacy-policy text and vendor table
  seed/                    seed importers (products, governorates)
  services/                Order/Inventory/Notification/Catalog/Auth/Queue
                            services, search_terms.py (Arabic + franco
                            catalog-search vocabulary), conversation_reset.py
                            (calls back into the assistant layer via a
                            registered callback, never a direct import --
                            see that module's docstring)
integrations/            everything that talks to an external vendor over
                          HTTP, one package per vendor
  shopify/
    client.py               GraphQL transport for the live Shopify path
    catalog.py, inventory.py, orders.py   live read/write
    files.py                the staged-upload dance (signed target -> bytes ->
                            a mutation naming where they landed), in one place
                            for both the dashboard's uploads and the size-chart
                            script
    size_charts.py          publish the charts to Shopify as metafields
    admin_customers.py, admin_orders.py, admin_products.py,
    admin_collections.py, admin_inventory.py                dashboard admin
    product_import.py       reconcile-on-boot for products created straight
                            in Shopify Admin
    webhook_registration.py subscribe Shopify to push order-status changes
    webhooks.py              inbound from Shopify: fulfilments and
                            cancellations -> order status -> the customer's
                            tracking message
  whatsapp/client.py        Meta Cloud API client
  instagram/
    client.py                Instagram Graph client (chunked text, no
                            templates, image-by-public-URL)
    token.py                 60-day token refresh
assistant/               the AI agent runtime, shared byte-for-byte by every
                          channel; never imports dashboard/
  runtime.py                entry point: handle_message(channel, external_id, text)
  dispatcher.py             debounce + worker threads; what keeps the webhook fast
  agent.py                  the tool-use loop
  context.py                what the model is *sent*: recent messages
                             verbatim, older ones compacted to what was
                             said. Not what is stored -- see session.py
  quoting.py                resolves a "reply to this message" against the
                             whole transcript, so the turn knows which
                             message it is about
  media.py                  voice notes and photos (see docs/MEDIA.md)
  interactive.py            tappable pickers, in a channel-neutral shape
  session.py                 DB-backed session storage
  display.py                 stored history -> bubbles a person can read;
                              shared by the harness and the dashboard
  media_serving.py           the local-file path guard both the harness and the
                              dashboard serve through (re-exports
                              common/servable_paths.py)
  providers/                  openrouter.py (default), gemini.py, fake.py
                              (tests/rehearsal), base.py
  tools/                      cart_tools, catalog_tools, order_tools, support_tools
  channels/whatsapp.py        the WhatsApp webhook + outbound sender registration
  channels/instagram.py       the Instagram twin: DMs (text, attachments,
                               quick replies) + comment ingest; comments ship OFF
  harness/                    local dev-only chat UI (web + terminal), unauthenticated
                              by design and OFF unless HARNESS_ENABLED=1
api/
  public_media.py           GET /public/media/{token}/... -- HMAC-gated public
                            URL for catalog files Meta must fetch itself
dashboard/                 staff dashboard, its own top-level package:
                            conversations, Shopify (products/
                            orders/customers), statistics, the review queue,
                            and feature-flag settings; staff-login
                            authenticated, ON by default
                            (DASHBOARD_SESSION_SECRET gates login).
                            web.py (auth + conversations) stays one file;
                            shopify_api.py / stats_api.py / queue_api.py /
                            settings_api.py / customers_api.py /
                            collections_api.py / inventory_api.py /
                            insights_api.py / inbox_api.py / staff_api.py are
                            sibling routers under the same guard, not a growing
                            single file -- see web.py's own docstring.
                            Every one of them goes through
                            guard.py::require_permission, one permission per
                            section (domain/services/staff_admin.py). The
                            sidebar hides what an account cannot open; that is
                            a courtesy, the route refusal is the control.
                            ranges.py parses the one date window both analytics
                            tabs use (presets, or an explicit start/end).
                            customer_filters.py holds the one filter vocabulary
                            the Customers screen offers, and customer_ledger.py
                            folds what a customer's orders come to -- orders
                            that stand, what they came to, orders cancelled,
                            what those came to, and the channels they bought
                            through -- out of the order list rather than off
                            Shopify's `numberOfOrders`/`amountSpent`, which are
                            one number each, count cancelled sales, and are
                            blank on every customer the backfill created. The
                            Customers screen's three tabs (all / bot / web) are
                            that one list segmented, not three routes.
                            dashboard.html is bilingual: Arabic is the source
                            language, `EN` is keyed on the Arabic, and the
                            tagged template `TR` translates only a template's
                            literal chunks -- never its `${...}` values, which
                            is where customer data arrives. Adding a screen
                            means adding its translations;
                            tests/test_dashboard_i18n.py fails otherwise.
                            QUICK_REPLIES is excluded on purpose: it is sent
                            to customers, not shown to staff.
                            inbox_api.py is read-only on purpose: every
                            outbound action still goes through web.py, so
                            there is one place a message can leave here. To
                            show conversations it reads assistant/session.py,
                            assistant/display.py, assistant/messages.py, and
                            assistant/media_serving.py directly -- the same
                            read-only surface the harness reads through --
                            plus domain/ services and integrations/ for
                            everything else; assistant/ never imports back
                            from dashboard/.
docs/                    ARCHITECTURE.md, OPERATIONS.md, MEDIA.md
data/                    products_seed.json, size_charts.json, governorates.json,
                          merge_catalog.py, images/, size-charts/ — catalog
                          metadata Shopify has no field for, NOT a duplicate
                          product database (price/stock come from Shopify)
scripts/                 shopify_sync.py (ongoing catalog/stock reconciliation),
                          shopify_backfill_customers.py (attach a customer to
                          orders placed before the bot did so -- orderCustomerSet,
                          dry-run by default),
                          shopify_set_skus.py (link local variant_id -> Shopify
                          SKU), shopify_check_live.py (read-only smoke check),
                          shopify_size_charts.py (publish the size charts to
                          Shopify as product metafields, for the storefront),
                          migrate_schema.py (add every column the models
                          declare and the database lacks — the general form),
                          migrate_add_shopify_order_columns.py (the earlier
                          one-time, SQLite-only version) — all dry-run by
                          default, idempotent, need --apply
theme/                   Liquid the Shopify theme uses, kept in the repo but
                          pasted in by hand: size-chart.liquid renders the
                          bilingual size guide from the metafields
                          scripts/shopify_size_charts.py writes. Nothing here
                          is loaded by the app -- see theme/README.md
tests/                   pytest suite (flat, one test_<module>.py per
                          subject rather than mirroring the source tree —
                          see README.md)
```

## Database

- PostgreSQL is the production database (`DATABASE_URL=postgresql+psycopg://...`).
  SQLite (`sqlite:///./wanas.db`) is fine for local development only.
- Chat/session history is persisted (`sessions` table / `assistant/session.py`)
  — this is a required production feature. **Never** remove it or replace it
  with an in-memory store. It is also append-only: a conversation *ending*
  (six hours idle, a staff reset, `HISTORY_CAP`) moves `sessions.context_start`
  forward and deletes nothing. Read the live slice with `session.load()` and
  the whole transcript with `session.transcript()` — the dashboard must use
  the latter, since a read must never be what ends a conversation. Only
  `session.purge()` deletes, and nothing calls it.
- **What is stored and what the model is sent are different lists.**
  `HISTORY_CAP` (150) bounds the live slice; `assistant/context.py` decides
  the provider's view — the last `MODEL_CONTEXT_MESSAGES` (24) verbatim plus
  up to `MODEL_CONTEXT_RECALL` (60) older messages compacted to what was
  actually said, tool calls and catalog dumps dropped. Conflating the two is
  what made the bot forget a product discussed ten minutes earlier: the cap
  counts messages, and one question costs four to six of them. Never
  summarise here — compaction removes whole messages only, so every sentence
  the model reads is the exact sentence that was said, and the verbatim
  window must never open on a `tool_results` whose call was compacted away.
- **A quoted reply is resolved, never guessed.** Every stored message carries
  the platform ids it was sent or received as (`mids`); outbound ids come
  from the send's response body and are stamped on by
  `session.attach_outbound_ids` after delivery. `assistant/quoting.py` looks
  a WhatsApp/Instagram `context.id` up across the *whole* transcript, archive
  included, and folds the original sentence into the turn. An unresolvable id
  is left unannotated on purpose: a reply to something older than the
  transcript is still an ordinary message, and inventing which one it was is
  exactly the wrong-message answer this closed.
- No Alembic — tables are created at startup via `Base.metadata.create_all`
  (`app.py`), which adds missing tables but **not** missing columns on
  existing ones. That gap cost four days of production orders
  (`orders.source_external_id`), so startup now also reconciles columns:
  `domain/schema_drift.py` compares the models against the live schema and
  `_ensure_schema_columns` in `app.py` adds what is missing, additively and
  idempotently (`AUTO_MIGRATE_SCHEMA=0` to only report it, and
  `python scripts/migrate_schema.py --apply` to do it by hand). A `NOT NULL`
  column with no server default is reported, never guessed at. Seed a new
  database with `python manage.py seed`.
- Important models: `Client`, `Product`, `Variant`, `Order`/`OrderItem`
  (carry Shopify order id/number columns), `ShippingRate`, `Staff`,
  `StaffQueueItem` (human-handoff / item-swap / alert queues),
  `WebhookEvent` (idempotency).

## Shopify

- Source of truth for **orders, live inventory, live price**. Do not build a
  second product database in Postgres for these fields.
- Postgres still legitimately holds catalog data Shopify has no field for
  (`style`, `department`, `collection`, size charts) — see
  `domain/services/catalog.py` (the overlay: Shopify numbers over local
  rows). `variant_id` <-> Shopify variant is matched by SKU.
- Product photos follow the same overlay as price/stock, not the
  style/department list above: `shopify_catalog.LiveVariant.image_url` (read
  alongside price/stock, same call) wins over the local `data/images/` file
  for that colour when staff have set one in Shopify Admin. Which regime
  `catalog._overlay_images` applies turns on *coverage*: once Shopify has a
  photo for **every** colourway, it is the photo set — the seeded paths for
  those colours are dropped, and a product `wanas.db` never split by colour
  gets its split from Shopify. Short of full coverage it stays additive, a
  Shopify photo leading a gallery that already had that key, never inventing
  a partial split. Which colour's photo actually gets *sent* is the
  `color` argument on the `get_variants` tool.
  `WhatsAppClient.send_image` sends an
  `http(s)` path by `link`; the local-file upload/cache path
  (`media_id_for`) is what still serves the twelve size charts and any
  product colour Shopify has no photo for yet. See `docs/ARCHITECTURE.md`.
- Anything deciding whether a sale may happen reads the **overlay**, never
  `variants.stock_qty` directly — that column is a seeded value nothing keeps
  current. `catalog.live_stock(variant)` is the one-variant form
  `add_to_cart` uses; reading the column there refused sizes Shopify was
  selling, and (because that refusal joins the stock waitlist) told those
  customers half an hour later that the item was "back in stock" when it had
  never left. A "back in stock" message must describe a *verified transition*
  — `StockWaitlistEntry.observed_stock` at or below zero then, above it now
  — never just a positive number today.
- Manual inventory decrement is deliberately **not** in the order path —
  Shopify already decrements on `orderCreate`; doing it twice would silently
  oversell.
- Order **status** comes back the other way: `integrations/shopify/webhooks.py`
  turns `orders/fulfilled` / `orders/cancelled` / `fulfillments/update` into
  `orders.advance_status`, which is what makes the tracking messages fire.
  Statuses move forward one stage at a time — never assign `order.status`
  directly.
- `SHOPIFY_ADMIN_TOKEN` and `SHOPIFY_WEBHOOK_SECRET` live only in `.env`.
  Never print, log, or commit either.

## LLM

- Provider: **OpenRouter** by default, via `assistant/providers/openrouter.py`,
  behind `assistant/providers/base.py`. Gemini (`gemini.py`) remains a valid
  alternate provider. Nothing above `assistant/providers/` may import a
  vendor SDK — every provider is called over raw HTTPS, so swapping is one
  new class + one config value (`LLM_PROVIDER`).
- Config: `LLM_PROVIDER`, `LLM_API_KEY` (or `GEMINI_API_KEY`/`OPENROUTER_API_KEY`),
  `LLM_MODEL` (or `GEMINI_MODEL`, blank lets the provider pick a working
  model). Voice notes transcribe via OpenAI Whisper (`OPENAI_API_KEY`);
  photos go through an OpenRouter vision model.
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
make harness              # or: python -m assistant.harness (terminal)
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
`WANAS_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/wanas make test`.
Without that opt-in variable set, an ambient `DATABASE_URL` is ignored and the
suite always runs on its own throwaway SQLite file (`tests/conftest.py`).

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
  WhatsApp or Instagram access tokens/app secrets, or any other credential —
  not in code, logs, commit messages, or chat output. The Instagram app
  secret is a *different string* from `WHATSAPP_APP_SECRET` even inside the
  same Meta app; never merge them.
- Never answer the shop's own Instagram comments or messages — an echo, a
  subscribed `message_echoes` delivery, or anything whose sender id equals
  the shop's account id is dropped before everything else. The bot replying
  to itself is the worst failure available on that channel.
- One private reply per Instagram comment, ever: `InstagramCommentReply` is
  written *before* the send precisely so a crash cannot permit a second one.
- Instagram outbound images must be public HTTPS URLs (Meta fetches them);
  local files go through the HMAC-gated `api/public_media.py` route, and
  `data/inbound` — customers' own photos and voice notes — must never be
  servable through it.
- A WhatsApp customer is **not always a phone number**. Since April 2026 Meta
  also sends a business-scoped user id (`messages[].from_user_id` /
  `contacts[].user_id`, e.g. `EG.1754797805572316`), and for a customer using a
  WhatsApp username it sends *only* that -- `from` and `wa_id` are omitted.
  `common/identifiers.py` is the one place that tells the two apart. Outbound,
  a BSUID goes in `recipient`, never `to`: `normalise_recipient` strips every
  non-digit, so sending one through `to` addresses a different person. Never
  run phone logic (`phone_variants`, auto-linking a returning customer) over an
  external_id without checking `is_phone_number` first.
- The outbound sender registry is per-channel. `get_sender()` with no channel
  is a bug in any new code: it silently means "the default channel", which is
  WhatsApp.
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
`DASHBOARD_SESSION_SECRET` / `python manage.py create-staff` in
`docs/OPERATIONS.md`.

**This is no longer read-only for Shopify.** The dashboard grew into the
single control/monitoring surface for the whole business: alongside
conversations it has a Shopify section (products — view, create, edit;
orders — view, fulfil, cancel, edit across *both* the bot and the website;
customers), store-wide statistics (KPIs, charts, built from a live Shopify
read since only bot orders ever reach Postgres — see
`domain/services/dashboard_stats.py`), the `item_swap`/`alert` review
queue, and staff-toggleable feature flags
(`domain/services/runtime_flags.py`), and a Team section where the owner adds
staff and ticks which sections each of them sees. A `Staff` row whose `role`
is NULL -- every account that existed before permissions shipped -- reads as
an **owner**, never as "scoped to nothing": the opposite locks everyone out of
the one screen that hands permissions out. Shopify is still the source of truth
for price/stock/orders; product create/edit pushes to Shopify first and
mirrors the wanas.db-only fields (`category`/`department`/`style`/
`collection`/`size_chart`) after — see the module docstring on
`integrations/shopify/admin_products.py` for exactly where that line
is drawn (no refunds — this shop is cash-on-delivery with nothing to
refund against — and removing a variant from an existing Shopify product
is deliberately left to Shopify Admin). Creating a product **does** take
pictures off the staff member's device: one per variant row, uploaded
through `POST /dashboard/api/shopify/uploads` and attached as that
colourway's *variant* image, which is the field the bot reads when a
customer names a colour. A size chart uploaded the same way sets
`custom.size_chart` on Shopify and `Product.size_chart_image` locally —
a chart picture with no published measurements behind it, which
`get_size_chart` answers as `image_only`.
