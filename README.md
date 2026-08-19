# Wanas Gallery — WhatsApp sales agent

An LLM agent that sells an Egyptian streetwear brand's catalog over WhatsApp
and places real orders on Shopify. It talks in Egyptian Arabic, understands
voice notes and photos, and cannot state a price, a size or an availability
that did not come from a tool.

```
Shopify           source of truth for orders, live inventory, live price
FastAPI (app.py)  the one deployed process
chatbot/          agent loop, tools, provider layer, channels, sessions, media
backend/          order/inventory/notification services, integrations, models
PostgreSQL        chat history, carts, clients, shipping rates, staff queue
```

- **Shopify** owns orders, stock and price. Selling calls `orderCreate`;
  Shopify decrements inventory, and the order appears in the admin tagged
  `chatbot` / `whatsapp` / `cash-on-delivery`.
- **PostgreSQL** is not a duplicate product database. It holds what Shopify has
  no field for — style, department, collection, size charts, per-colour photos
  — plus everything conversational.
- **Gemini** sits behind a provider abstraction; nothing above
  `chatbot/providers/` imports a vendor SDK.

Business rules (variant/pricing maths, shipping, sizing) are in
[`AGENTS.md`](AGENTS.md). Repository conventions are in
[`CLAUDE.md`](CLAUDE.md). Deeper documentation:

| | |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it fits together and the five decisions behind it |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Deploying, webhooks, tuning, reading the logs |
| [docs/MEDIA.md](docs/MEDIA.md) | Voice notes and photos |
| [CHANGELOG.md](CHANGELOG.md) | What changed and why |

## Quick start

```bash
make dev                      # dependencies, including test and lint tools
cp .env.example .env
make seed                     # import the catalog and the governorate list
make harness                  # browser chat UI at /harness
make test
```

With no LLM key the harness runs a rehearsal stand-in that maps typed commands
(`products hoodie`, `variants wanas-hoodie`, `gov`, `add <variant_id>`) to tool
calls. Set `LLM_PROVIDER=gemini` and a key to get the real agent.

`make help` lists the rest. Useful CLI:
`python -m backend.cli <seed|set-fee|create-staff|catalog-report>`.

## What it can do

- **Browse and sell.** Search the catalog in Arabic, franco-Arabic or English
  (`هودي أسود`, `hoodi olive`, `الهودي الزيتي` all resolve), quote real
  per-variant prices, send one product photo, answer sizing from the published
  chart only, take an address, and place a cash-on-delivery order.
- **Voice notes.** Transcribed and answered like any other message.
- **Photos.** Read against a shortlist built from the real catalog. The reading
  is a hint about which tool to call — never a claim about stock. See
  [docs/MEDIA.md](docs/MEDIA.md).
- **A tappable governorate picker**, in two steps (region, then governorate),
  because twenty-seven governorates do not fit in one WhatsApp list and the
  governorate sets the shipping fee.
- **Order tracking that is actually true.** Shopify webhooks move the order
  through packed → shipped → delivered and push the message for each stage.
- **After the sale.** Change a quantity, cancel before shipping, request an
  item swap (staff decide), rate a delivered order.
- **Hand over to a person** for complaints, anything out of scope, or anything
  it could not read — and a staff dashboard (`/dashboard`, its own login) to
  see who is waiting and answer them, alongside Shopify products/orders/
  customers, store-wide statistics, the swap/alert review queue, and the
  bot's own feature-flag settings. See "The staff dashboard" in
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Testing

```bash
make test
```

Against PostgreSQL — worth doing before deploying, since the concurrency test
is the one that depends most on the database:

```bash
DATABASE_URL=postgresql+psycopg://user:pass@localhost/wanas make test
```

Opt-in live-model tests (these cost real quota and are skipped by default):

```bash
RUN_LIVE_TESTS=1 pytest tests/test_conversation_live.py -v
```

## Production

Railway start command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

PostgreSQL is required. There is no Alembic — tables are created at startup;
run `seed` / `set-fee` / `create-staff` once against a new database. Register
the Meta webhook (`POST /webhooks/whatsapp`) and the Shopify webhooks
(`POST /webhooks/shopify`) once the app is reachable over HTTPS. Staff log in
at `/dashboard` with the account `create-staff` made.

The full pre-launch checklist, including what silently does nothing when it is
missing, is in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Environment

See [`.env.example`](.env.example) for every variable with its default and the
behaviour when it is unset. Required for a real deployment: `DATABASE_URL`,
`LLM_PROVIDER`, `LLM_API_KEY`, `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`,
`SHOPIFY_WEBHOOK_SECRET`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`,
`WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `DASHBOARD_SESSION_SECRET`.

Never commit `.env` — it is git-ignored. `.env.example` holds names and safe
placeholders only.
