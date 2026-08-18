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
| `data/` | Catalog metadata Shopify has no field for. Not a product database. |
| `scripts/` | Shopify maintenance. All dry-run by default, idempotent, need `--apply`. |

## Known gap

`request_human` pauses a conversation and writes a handoff record, but there is
no staff UI to resolve one — the dashboard that used to do it was removed, and
only the dev harness's `/unpause` stands in. This is deliberate and documented,
not something to quietly fix by rebuilding a dashboard.

Until it is closed, every path that ends in a handoff ends in a conversation
that stays paused. That is worth knowing before enabling anything that
increases how often handoffs happen.
