# Wanas Gallery — Shopify chatbot

A WhatsApp LLM agent that sells an Egyptian streetwear brand's catalog and
places real orders on Shopify.

## Architecture

```
Shopify    -- source of truth for orders, live inventory, and live price
FastAPI (app.py) -- the one deployed process (modular monolith)
chatbot/   -- agent loop, tools, provider layer, WhatsApp channel, sessions
backend/   -- Order/Inventory/Notification services, Shopify integration, DB models
PostgreSQL -- chat/session history, carts, shipping rates, staff, audit log,
              plus catalog metadata Shopify has no field for (style,
              department, collection, size charts, per-colour photos)
```

- **Shopify** owns orders, live stock, and live price. Selling something
  calls Shopify's `orderCreate`; Shopify decrements inventory, and the order
  shows up in Shopify admin tagged `chatbot` / `whatsapp` / `cash-on-delivery`.
- **PostgreSQL** is not a duplicate product database. It holds fields
  Shopify has no place for, plus chat/session history, carts, shipping
  rates, staff, and the audit/handoff queue. Price and stock are read live
  from Shopify per message; if Shopify is unreachable the bot falls back to
  the local numbers and logs a warning once.
- **Gemini** is the LLM provider, behind a provider abstraction
  (`chatbot/providers/`) — nothing above that layer imports a vendor SDK.

Business rules (variant/pricing math, shipping, sizing) live in `AGENTS.md`.
Repository conventions and constraints for Claude Code sessions live in
`CLAUDE.md`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Every value in `.env` has a working default or a documented "not
configured" behaviour — the app boots and the local harness works with none
of them filled in. See `.env.example` for what each variable does.

## Running locally

```bash
python -m backend.cli seed              # import catalog + governorates
python -m chatbot.harness.web            # browser chat UI at /harness
# or: python -m chatbot.harness          # terminal harness
uvicorn app:app --reload                 # full app, incl. the WhatsApp webhook
```

With no LLM key the harness runs a rehearsal stand-in that maps typed
commands to tool calls. With `LLM_PROVIDER=gemini` and a key it's the real
agent.

Useful CLI commands: `python -m backend.cli <seed|set-fee|create-staff|catalog-report>`.

Shopify-side maintenance scripts live in `scripts/` (all dry-run by default,
idempotent, need `--apply`): `shopify_sync.py` reconciles the local catalog
against Shopify, `shopify_check_live.py` is a read-only smoke check,
`shopify_set_skus.py` links local variant IDs to Shopify SKUs.

## Testing

```bash
python -m pytest tests/ -q
```

Against PostgreSQL (worth doing before deploying — the concurrency test
depends most on the database):

```bash
DATABASE_URL=postgresql+psycopg://user:pass@localhost/wanas python -m pytest tests/ -q
```

Opt-in live-model tests (cost real quota, skip by default):

```bash
RUN_LIVE_TESTS=1 python -m pytest tests/test_conversation_live.py -v
```

## Production

Start command (Railway):

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

PostgreSQL is required in production (`DATABASE_URL` as
`postgresql+psycopg://...`). There's no Alembic — tables are created at
startup; run `seed` / `set-fee` / `create-staff` once against a new
database. Register the WhatsApp webhook (`POST /webhooks/whatsapp`) with
Meta once the app is reachable over HTTPS.

Before anything is reachable by a real customer:

- `HARNESS_ENABLED=0` — the local chat harness is unauthenticated by design
- `CHATBOT_DEBUG=0` — raw provider errors must never reach a customer reply
- Real shipping fees set for every governorate you deliver to
- Meta WhatsApp templates approved for proactive messages (confirmation,
  status pushes, feedback request); until approved they go out as free-form
  text, which only works for verified test recipients

## Environment variables

See `.env.example` for the full list with defaults and notes. Required for a
real deployment: `DATABASE_URL`, `LLM_PROVIDER`, `LLM_API_KEY`,
`SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`.

Never commit `.env` — it's git-ignored. `.env.example` holds names and safe
placeholders only.
