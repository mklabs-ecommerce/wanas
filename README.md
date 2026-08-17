# Wanas Gallery — Shopify chatbot

A WhatsApp LLM agent that sells an Egyptian streetwear brand's catalog and
places real orders on Shopify. Business rules and data assumptions live in
`AGENTS.md`; day-to-day working context for future Claude Code sessions lives
in `CLAUDE.md`. This file is how to run it.

## Architecture

```
Shopify   -- source of truth for orders, live inventory, and live price
   |
FastAPI (app.py)  -- the one deployed process (modular monolith)
   |
chatbot/  -- agent loop, tools, provider layer, WhatsApp channel, session storage
   |
backend/  -- Order/Inventory/Notification services, Shopify integration, DB models
   |
PostgreSQL -- chat/session history, carts, shipping rates, staff, audit log,
              plus catalog metadata Shopify has no field for (style, department,
              collection, size charts, colour photos)
```

- **Shopify** owns orders, live stock, and live price. When the bot sells
  something it calls Shopify's `orderCreate` — Shopify decrements inventory,
  and the order shows up in the Shopify admin next to storefront orders,
  tagged `chatbot` / `whatsapp` / `cash-on-delivery`.
- **PostgreSQL** is not a duplicate product database. It holds the fields
  Shopify has no place for (style, department, collection, size charts,
  per-colour photos), plus everything that's genuinely local: chat/session
  history, carts, shipping rates, staff, the audit log, and the human-handoff
  queue. Product price and stock are read live from Shopify at message time;
  if Shopify is unreachable the bot logs a warning once and falls back to the
  local catalog numbers rather than failing the conversation.
- **Gemini** is the LLM provider, behind a provider abstraction
  (`chatbot/providers/`) — nothing above that layer imports a vendor SDK, so
  swapping providers means writing one class and changing one config value.
- The old custom storefront (`web/`, `storefront/`) and the staff dashboard
  (`dashboard/`) that shipped in Phase 1 have been **removed**. The store now
  runs on a Shopify theme; Shopify Admin is where products, inventory, and
  orders are managed. See "Legacy" in `CLAUDE.md`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Every value in `.env` has a working default or a documented "not configured"
behaviour — the app boots and the harness works with none of them filled in.
Fill in `LLM_API_KEY` (Gemini) and `SHOPIFY_ADMIN_TOKEN` to exercise the real
paths; see `.env.example` for what each variable does.

## 1. Database

```bash
python -m backend.cli seed
```

Imports the catalog (`data/products_seed.json`) and the 27 governorates.
Expect `18 products, 208 variants, 114 in stock`. Re-running the seed is
safe — it updates catalog fields in place and never overwrites stock you've
already set locally or a shipping fee.

```bash
python -m backend.cli catalog-report
python -m backend.cli set-fee <governorate> <fee>
python -m backend.cli create-staff <username>   # for audit-log attribution
```

## 2. Backend

A library the chatbot calls — nothing to run on its own. Its guarantees are
covered by tests:

```bash
python -m pytest tests/test_order_transaction.py tests/test_backend_services.py -v
```

## 3. Shopify

Set `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `SHOPIFY_API_VERSION` in
`.env`. Without credentials the catalog serves the local database's own
price/stock — degraded, not broken, and logged once.

```bash
python scripts/shopify_set_skus.py     # write variant_id -> SKU (run once, first)
python scripts/shopify_check_live.py   # read-only smoke check
python scripts/shopify_sync.py         # compare local catalog vs Shopify, --apply to fix
```

All scripts under `scripts/` are dry-run by default and idempotent — pass
`--apply` to write.

## 4. Chatbot — the local harness

Two surfaces, one entry point. Both call the same
`handle_message(channel, external_id, text)` the WhatsApp webhook calls —
there is no second agent and nothing WhatsApp-shaped in either of them.

```bash
python -m chatbot.harness.web   # browser chat window at /harness
python -m chatbot.harness       # terminal, `help` for commands, -v for tool tracing
```

With no LLM key the terminal harness runs a rehearsal stand-in that maps
typed commands to tool calls. With `LLM_PROVIDER=gemini` and a key it's the
real agent.

It is **unauthenticated by design** — `HARNESS_ENABLED=0` leaves it out of
the app entirely, and the app logs a warning on every boot while it's
mounted. Never leave it on in a deployment reachable by anyone else.

## 5. WhatsApp

Fill in `WHATSAPP_*` in `.env`, then:

```bash
uvicorn app:app --reload
```

Webhook: `POST /webhooks/whatsapp` (`GET` answers Meta's verification
handshake). Until credentials exist, the webhook returns 503 and outbound
messages are logged instead of sent. `GET /health` reports which parts are
configured.

## Tests

```bash
python -m pytest tests/ -q
```

To run against PostgreSQL (worth doing before deploying, since the
concurrency test depends most on the database):

```bash
DATABASE_URL=postgresql+psycopg://user:pass@localhost/wanas python -m pytest tests/ -q
```

Conversation-behaviour tests against the real model are opt-in (cost real
quota) and skip by default:

```bash
RUN_LIVE_TESTS=1 python -m pytest tests/test_conversation_live.py -v
```

### Checking the architectural boundaries

```bash
grep -rn "from chatbot\|import chatbot" backend/
grep -rln "genai\|openai\|anthropic" backend/ app.py
```

Both should print nothing — `/chatbot/` calls into `/backend/`, never the
reverse, and nothing outside `chatbot/providers/` imports a vendor SDK.

## Production (Railway)

Start command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

PostgreSQL is required in production — set `DATABASE_URL` to a
`postgresql+psycopg://` URL (not plain `postgresql://`). Tables are created
at startup via `Base.metadata.create_all`; there's no Alembic in this
project, so run `seed` / `set-fee` / `create-staff` once against the new
database after first deploy.

Before anything is reachable by a real customer:

- `HARNESS_ENABLED=0`
- `CHATBOT_DEBUG=0` (raw provider errors must never reach a customer reply)
- Real shipping fees set for every governorate you deliver to
- Meta WhatsApp templates approved for proactive messages (confirmation,
  status pushes, feedback request) — otherwise they go out as free-form text,
  which only works for verified test recipients

See `PRODUCTION-DEPLOYMENT.md` for the full walkthrough (Railway + Postgres +
custom domain + Meta webhook registration).

## Environment variables

See `.env.example` for the full list with defaults and notes. Required for a
real deployment: `DATABASE_URL`, `LLM_PROVIDER`, `LLM_API_KEY`,
`SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`.

Never commit `.env` — it's git-ignored. `.env.example` holds names and safe
placeholders only.
