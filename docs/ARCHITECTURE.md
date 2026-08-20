# Architecture

How the pieces fit, and why they are arranged this way. Business rules live in
[`AGENTS.md`](../AGENTS.md); day-to-day operations in
[OPERATIONS.md](OPERATIONS.md).

## The shape of it

```
                      ┌───────────────────────────────────────────────┐
   WhatsApp  ────────►│  POST /webhooks/whatsapp                      │
   (Meta Cloud API)   │    verify signature → claim message id →      │
                      │    download media → hand to the dispatcher    │
                      │                             ↓ returns 200     │
                      └───────────────────────────────────────────────┘
                                                    │
                              chatbot/dispatcher.py │ debounce ~6s,
                              (worker threads)      │ one turn per conversation
                                                    ▼
                      ┌───────────────────────────────────────────────┐
                      │  chatbot/runtime.py  handle_message()          │
                      │    pause flag · voice → transcript             │
                      │    photo → reading · one Shopify snapshot      │
                      └───────────────────────────────────────────────┘
                                                    ▼
                      ┌───────────────────────────────────────────────┐
                      │  chatbot/agent.py   the tool-use loop          │
                      │    Gemini ⇄ 18 tools, capped at 8 rounds       │
                      └───────────────────────────────────────────────┘
                            │                              │
              chatbot/tools/*                      backend/services/*
              (the refusals)                       (the business rules)
                                                           │
                                    ┌──────────────────────┴─────────────┐
                                    ▼                                    ▼
                            PostgreSQL                              Shopify
                    sessions · carts · clients               price · stock · orders
                    orders · shipping rates                          │
                    staff queue · webhook events                     │
                                    ▲                                │
                      POST /webhooks/shopify ◄───────────────────────┘
                      fulfilled / cancelled → status → WhatsApp push
```

One deployed process (`uvicorn app:app`), internal modules. At this volume five
separately deployed services would be more operational overhead than the
problem needs.

## The five decisions everything else follows from

### 1. A tool that refuses is a guarantee; a prompt instruction is a preference

The model handles understanding and phrasing. It has no access to the catalog,
the cart or the orders except through the tool registry, and **every fact in
every reply comes from a tool result**.

That is not enforced by asking nicely. `variant_id` cannot be guessed, so
`get_variants` has to be called before anything can be added to a cart.
`confirm_order` re-reads live stock itself rather than trusting the
conversation. `validate_arguments` rejects an unknown argument instead of
ignoring it, so a hallucinated `price` cannot look like it was honoured.

Anything in the prompt's "never" list that is *not* backed by a tool that
refuses is a bug in the design, not a bug in the prompt.

### 2. Shopify owns price, stock and orders

Read live per message (`backend/services/shopify_catalog.py`), matched to the
local catalog by SKU. Selling calls `orderCreate` and **Shopify** decrements
inventory — there is deliberately no second decrement here, because doing it
twice oversells silently.

Postgres still holds what Shopify has no field for: `style`, `department`,
`collection`, size charts, per-colour photos. That is not duplicate product
data.

When Shopify is unreachable the browse path falls back to the local numbers and
logs once; the **order** path refuses (`store_unavailable`) rather than
promising stock it could not check.

**Product photos follow the same rule as price.** `wanas.db`/`data/images/`
was this project's starting point — this repo is a sample built to show the
idea, not a deployment for one specific shop, so it shipped with a scraped
photo set rather than a live store's own CDN. Once staff attach a photo to a
variant or product in Shopify Admin, `shopify_catalog.fetch_all` reads it back
alongside price and stock (`LiveVariant.image_url`), and
`catalog.get_variants` puts it ahead of the local file for that colour —
never inventing a colour split the local gallery does not already have; see
the docstring on `catalog._overlay_images`. `WhatsAppClient.send_image` sends
an `http(s)` path by `link` directly rather than uploading it through Meta's
media endpoint, since Meta fetches the link itself — the upload-and-cache path
(`media_id_for` / `WhatsAppMedia`) still exists for whatever is a genuine local
file, chiefly the twelve size charts. No separate image host was added: the
store that is already the source of truth for price and stock is also the
simplest place for its own photos to live.

### 3. The provider is a boundary, not a dependency

Nothing above `chatbot/providers/` imports a vendor SDK. Gemini is called over
raw HTTPS. Swapping providers is one new class and one config value
(`LLM_PROVIDER`), because cost is the reason it may change.

The neutral message format (`chatbot/messages.py`) has three shapes and carries
an opaque `signature` blob through the database untouched — that is what makes
a second tool call in the same conversation work on models that demand their
own signatures back.

### 4. The webhook accepts; the worker answers

`POST /webhooks/whatsapp` verifies, claims the message id in its own committed
transaction, downloads media, and returns 200 — in milliseconds. The agent turn
runs on a worker thread after a short debounce window
(`chatbot/dispatcher.py`).

Two reasons. An `async` endpoint running a thirty-second synchronous model call
blocks the event loop for every other conversation in the process. And
customers type in fragments: "عايز هودي" / "أسود" / "لارج" inside five seconds
is one request, and answering it three times costs three model calls and reads
like three different people.

The dispatcher is in-process, which fits one Railway instance. Two instances
would debounce independently — still correct, just less effective. Replacing it
with Redis means replacing that one file.

### 5. Media is understood below the model's reach

A voice note is transcribed and enters the pipeline as ordinary text. A photo
is read against a shortlist built from the real catalog, and the result is
handed to the agent as a *note*, never as an answer — carrying the product
**name** (safe to say to a customer) rather than its id, and telling the agent
in as many words to verify with the tools before quoting anything.

Every failure in that path — no key, a provider that cannot listen or see, an
unreadable file, a low-confidence reading of something we do not stock — falls
back to the human handoff, which is what happened to *every* photo and voice
note before this existed.

## Where things live

| Path | What it is |
| --- | --- |
| `app.py` | Composition root. The only file that wires the pieces together. |
| `backend/config.py` | Every setting, read from the environment. No hardcoded credentials. |
| `backend/models.py` | The ORM. `Client`, `Product`, `Variant`, `Order`, `ShippingRate`, `StaffQueueItem`, `WebhookEvent`. |
| `backend/services/` | Business rules. `orders.py` owns "can this order happen?". |
| `backend/services/search_terms.py` | Arabic and franco vocabulary for catalog search. |
| `backend/integrations/` | Outbound clients: Shopify GraphQL, Meta Cloud API. |
| `backend/webhooks/shopify.py` | Inbound from Shopify: fulfilments and cancellations. |
| `chatbot/runtime.py` | `handle_message()` — the entry point every channel calls. |
| `chatbot/dispatcher.py` | Debounce and worker threads. |
| `chatbot/agent.py` | The tool-use loop, and the reply sanitisers. |
| `chatbot/media.py` | Voice notes and photos. |
| `chatbot/prompt.py` | Persona, flow, and the data quirks the model would otherwise get wrong. |
| `chatbot/interactive.py` | Tappable pickers, in a channel-neutral shape. |
| `chatbot/tools/` | The eighteen tools and their refusals. |
| `chatbot/channels/whatsapp.py` | The only WhatsApp-specific code in the conversational path. |
| `chatbot/harness/` | Dev-only chat UI. Unauthenticated by design; off unless `HARNESS_ENABLED=1`. |
| `dashboard/` | Staff dashboard: conversations, Shopify (products/orders/customers), statistics, the review queue, settings. See below. |
| `chatbot/display.py` | Turning stored history into bubbles a person can read — shared by the harness and the dashboard. |
| `data/` | Catalog metadata Shopify has no field for. Not a product database. |
| `scripts/` | Shopify maintenance. All dry-run by default, idempotent, need `--apply`. |

## The staff dashboard

`request_human` (`chatbot/tools/support_tools.py`) has always paused a
conversation and written a `StaffQueueItem` of kind `handoff`. For a long
stretch nothing read that queue back or un-paused the conversation except the
dev harness's `/unpause` stand-in — a customer who triggered a handoff stayed
stuck until someone edited the database by hand. `dashboard/` is the
other half: `GET /dashboard` lists conversations (waiting-on-staff ones first,
oldest wait first), shows one in full, and lets a logged-in staff member
either reply to a **paused** one — which sends the customer a real WhatsApp
message through the same `OutboundSender` port the bot's own replies use, then
resolves the queue item and un-pauses the conversation in one transaction —
or resolve it without a reply (a false alarm, or a customer already handled by
phone).

It is authenticated, unlike the harness: `Staff` accounts already existed
(`backend/services/auth.py`, `python -m backend.cli create-staff`) with
nowhere to log in. The session is a signed cookie
(`auth.issue_session_token`/`staff_from_session_token`, HMAC over the
standard library) rather than a sessions table — one fewer thing that has to
survive a restart for a shop this size. With no `DASHBOARD_SESSION_SECRET`
configured, login refuses outright (503) rather than signing cookies with a
secret that changes every process restart — the same call
`backend/webhooks/shopify.py` makes with no webhook secret set.

The dashboard cannot reply into a conversation the bot still owns: `reply`
checks `identities.is_paused` first and refuses (409) otherwise. A person and
the model both writing into the same live turn is exactly the two-writers race
the debounce lock (`chatbot/dispatcher.py`) exists to prevent, and the fix is
the same one — exactly one writer at a time, staff included.

### It grew into the whole business's control surface

Conversations was the first section; it is no longer the only one. The same
staff login now also covers:

- **Shopify** (`dashboard/shopify_api.py`) — products (view,
  create, edit), orders (view, fulfil, cancel, edit quantity), customers.
  Full read/write, not the read-only mirror the dashboard started as.
- **Statistics** (`stats_api.py`, `backend/services/dashboard_stats.py`) —
  revenue, orders, AOV, best-sellers, a status breakdown, charted inline
  (hand-rolled SVG, no chart library — the dashboard stays zero-build).
- **The review queue** (`queue_api.py`) — `item_swap` and `alert`, the two
  `StaffQueueItem` kinds that had full backend logic (`orders.apply_swap`,
  `notifications.low_stock_breach` and friends) and no UI at all until this,
  the same gap `handoff` was in before this dashboard existed.
- **Settings** (`settings_api.py`, `backend/services/runtime_flags.py`) —
  the voice/image/interactive-list feature flags, toggleable without a
  redeploy, and a read-only system-status panel.
- **Customers, the WhatsApp side** (`customers_api.py`) — the local
  `Client` table, kept as a *separate* view from Shopify's customers rather
  than merged, for the same reason stats reads Shopify directly: a customer
  who only ever ordered on the website has no `Client` row, and pretending
  otherwise would double- or under-count.

Two decisions shape all of it. First: **Shopify orders and stock still win**.
Store-wide reads (orders, customers, stats) go straight to Shopify — see
`shopify_admin_orders.py` / `shopify_admin_customers.py` / `dashboard_stats.py`
— because the local `orders` table only ever holds what the *bot* sold; a
number built from Postgres alone would silently miss every website sale.
Second: **write actions prefer the existing order service when a local row
exists**. Cancelling or editing an order the bot placed goes through
`backend/services/orders.py`'s `cancel()` / `modify_quantity()` — already
transactional, already notifies the customer — rather than calling
`shopify_orders` a second, divergent way; only a pure website order (no local
row) talks to Shopify directly from the dashboard route.

Product create/edit (`shopify_admin_products.py`) pushes to Shopify first,
then mirrors the wanas.db-only fields onto local `Product`/`Variant` rows —
the same overlay direction `catalog.py` already uses for price and stock,
just for the fields that run the other way. A product created only on
Shopify through this dashboard is invisible to the bot's own search until
that mirror runs, so the create/edit forms ask for both halves together
rather than leaving that a manual follow-up step.

Deliberately out of scope, and disclosed rather than silently missing:
**refunds** (this shop is cash-on-delivery with no captured payment
transaction to refund against — `cancel()` with restock is the real "undo"),
**product image upload** (attach-by-URL only; a real file upload needs
`stagedUploadsCreate` and is a clearly-flagged fast-follow), and **removing
a variant from an existing Shopify product** (destructive to order history
in a way nothing here has a story for yet — do that in Shopify Admin).
