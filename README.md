# Wanas Gallery — Phase 1

B2C clothing e-commerce for an Egyptian shop: a WhatsApp LLM agent that takes
orders, and a staff dashboard behind it. Design lives in `docs/components/`;
scope and data assumptions live in `AGENTS.md`. This file is only how to run it.

## Layout

```
app.py            composition root — the one place the pieces are wired together
backend/          Order, Inventory, Notification services + the schema
chatbot/          agent loop, tools, provider layer, session storage, channels
dashboard/        staff UI (server-rendered templates)
data/             the seed (do not re-run merge_catalog.py — see AGENTS.md)
tests/
```

`/chatbot/` calls into `/backend/`, never the reverse. Nothing above
`chatbot/providers/` imports a vendor SDK. Both are checked by grep in one
command at the bottom of this file.

## Setup

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Every value in `.env` has a working default. **Nothing below needs a
credential** — the four things that are still missing (Meta app, LLM key,
shipping fees, staff login) each degrade to a documented behaviour rather than
blocking anything.

## 1. Database

```bash
python -m backend.cli seed
```

Expect `18 products, 208 variants, 114 in stock` and `27 governorates seeded`.

```bash
python -m backend.cli catalog-report
```

Re-running the seed is safe: it updates catalog fields in place and never
overwrites stock or a shipping fee you have already set.

## 2. Backend

Nothing to run on its own — it is a library the chatbot and dashboard call.
Its guarantees are covered by tests:

```bash
python -m pytest tests/test_order_transaction.py tests/test_backend_services.py -v
```

## 3. Chatbot — the local harness

Two surfaces, one entry point. Both call the same
`handle_message(channel, external_id, text)` the WhatsApp webhook calls —
there is no second agent, no duplicated tool logic, and nothing WhatsApp-shaped
in either of them.

### Web UI (a browser chat window)

```bash
python -m chatbot.harness.web
```

Then open **<http://127.0.0.1:8000/harness>**. It prints the URL on startup.
`--port 8011` and `--reload` are accepted. If the app is already running under
uvicorn, `/harness` is served by it — this command is only a shortcut that
starts the app and tells you where to look.

Right-to-left Arabic chat, and next to each reply it shows what actually
happened: which tools were called, any refusal with its full payload (an
`out_of_stock` is only actionable with its `alternatives`), size-chart images
inline, and proactive notifications — the order confirmation, status pushes —
in the transcript rather than buried in a log. The identity box at the top
switches customers; each one has its own conversation and cart. There are
buttons to reset the session, simulate an incoming photo, and stand in for the
staff resolve that un-pauses a handed-off conversation.

It is **unauthenticated by design**: anyone who can reach it can converse as
any customer identity. `HARNESS_ENABLED=0` leaves it out of the app entirely,
and the app logs a warning on every boot while it is mounted.

### Terminal

```bash
python -m chatbot.harness
```

It calls the same `handle_message(channel, external_id, text)` the WhatsApp
webhook calls. With no key it runs the **rehearsal stand-in**, which maps typed
commands to tool calls (type `help`). That is scaffolding for exercising the
flow, not the product — with `LLM_PROVIDER=gemini` and a key it is the real
agent and you write like a customer.

A full order, start to finish:

```
categories
products hoodie
variants wanas-hoodie
add wanas-hoodie-s-olive 2
ship القاهرة
order Omar Ali | Cairo | 12 Test Street, Apt 4 | 01000000000
```

`ship` will say `no_rate_set` until a fee exists — that refusal is the point.
Set one first:

```bash
python -m backend.cli set-fee Cairo 60
```

Other terminal commands: `/cart`, `/reset`, `/image <path>` (simulates an
incoming photo, which goes straight to a human), `/unpause` (simulates the
staff resolve that hands a paused conversation back), `/quit`, `-v` for tool
call tracing.

## 4. WhatsApp

Fill in `WHATSAPP_*` in `.env`, then:

```bash
uvicorn app:app --reload
```

The webhook is `POST /webhooks/whatsapp`; `GET` on the same path answers Meta's
verification handshake. Expose it with a tunnel and register it against your
Meta app with `WHATSAPP_VERIFY_TOKEN`.

Until the credentials exist the webhook returns 503 and outbound messages are
logged instead of sent — everything else, including the harness, works
unchanged. `GET /health` says which of the two is true.

Templates: proactive messages (confirmation, status pushes, feedback request)
need Meta-approved templates before launch, and approval takes days to weeks.
Until then they go out as free-form text, which works for verified test
recipients. Each one logs that it did.

## 5. Dashboard

```bash
python -m backend.cli create-staff amira
uvicorn app:app --reload
```

Then <http://localhost:8000/dashboard>. There is deliberately no default
account: a shared or seeded login destroys the attribution this one-role model
relies on.

Screens: orders (list, detail, status advance, cancel), products with the
per-variant grid, clients, shipping rates, size-chart assignment, the three
queues (item swap, human handoff, alerts), and the audit log.

## Tests

```bash
python -m pytest tests/ -q
```

Covering what `AGENTS.md` asks for, plus the surrounding paths:

| File | What it pins down |
|---|---|
| `test_seed_import.py` | 18 / 208 / 114, the taxonomy, the chart mapping, idempotent re-import |
| `test_order_transaction.py` | Two concurrent orders for the last unit; a mid-transaction crash leaves no stock decremented and no order written |
| `test_backend_services.py` | Atomic decrement, per-colour pricing, governorate matching, the confirmed-link rule, notifications |
| `test_tool_contracts.py` | Every refusal in `15-tool-contracts.md`, and that there are exactly seventeen tools |
| `test_agent_and_session.py` | Session trimming (starts at a user message, never splits a tool-call pair), the loop cap, the pause flag, the image rule |
| `test_whatsapp_channel.py` | Handshake, signature, idempotency, photo handling |
| `test_dashboard.py` | No route reachable unauthenticated, CSRF, attribution, first-resolve-wins, swap approval |
| `test_gemini_provider.py` | Key formats, model resolution, thought-signature replay across turns, payload debugging |
| `test_harness_web.py` | The web UI calls the real entry point, surfaces refusals, and serves nothing outside the catalog |
| `test_conversation_behavior.py` | Product photos really attach, no path reaches a customer, a compound request lands in one pass, the prompt still bans what it was written to ban |

### Conversation behaviour against the real model

Judgement — asking instead of picking a product, not re-asking for a size the
customer already gave — cannot be tested without a model. Those live in
`test_conversation_live.py` and are opt-in, because they cost real quota:

```bash
RUN_LIVE_TESTS=1 python -m pytest tests/test_conversation_live.py -v
```

They skip by default. The assertions check the shape of the behaviour, never
exact wording: different phrasing is fine, picking a product at random is not.

To run the same suite against PostgreSQL — worth doing before launch, since the
concurrency test is the one that most depends on the database:

```bash
DATABASE_URL=postgresql+psycopg://user:pass@localhost/wanas python -m pytest tests/ -q
```

## Checking the two architectural boundaries

```bash
grep -rn "from chatbot\|import chatbot" backend/ ; grep -rln "genai\|openai\|anthropic" backend/ dashboard/ app.py
```

Both should print nothing.

## Still to fill in

- Meta app credentials + verified test recipient numbers → `.env`
- LLM API key and model → `.env` (`LLM_PROVIDER=gemini`)
- Shipping fees for all 27 governorates → dashboard, or `backend.cli set-fee`
- A staff login → `python -m backend.cli create-staff <username>`

No email provider is needed: the WhatsApp flow never collects an email, so the
customer confirmation is WhatsApp-only and staff alerts go to the dashboard's
alert inbox.

## Not built (deliberately)

Website, Facebook/Instagram/TikTok DM, public comment auto-reply, discount
codes, image recognition, analytics, and the Payment service — cash on delivery
only, so there is no gateway, no `Pending payment` status and no timeout job.
See "Explicitly out of scope" in `AGENTS.md`.
