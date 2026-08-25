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
                       ┌───────────────────────────────────────────────┐
   Instagram ────────►│  POST /webhooks/instagram                     │
   (Instagram Login)  │    same shape as the WhatsApp box above:      │
        DMs           │    signature (IG app secret) → claim `ig:` →  │
        + comments    │    download attachments → own dispatcher      │
                       │                             ↓ returns 200     │
                       └───────────────────────────────────────────────┘
                                                     │
                               assistant/dispatcher.py │ debounce ~6s,
                               (worker threads)      │ one turn per conversation
                                                     ▼
                       ┌───────────────────────────────────────────────┐
                       │  assistant/runtime.py  handle_message()          │
                       │    pause flag · voice → transcript             │
                       │    photo → reading · one Shopify snapshot      │
                       └───────────────────────────────────────────────┘
                                                     ▼
                       ┌───────────────────────────────────────────────┐
                       │  assistant/agent.py   the tool-use loop          │
                        │    the LLM ⇄ 18 tools, capped at 8 rounds     │
                       └───────────────────────────────────────────────┘
                             │                              │
               assistant/tools/*                      domain/services/*
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

Read live per message (`integrations/shopify/catalog.py`), matched to the
local catalog by SKU. Selling calls `orderCreate` and **Shopify** decrements
inventory — there is deliberately no second decrement here, because doing it
twice oversells silently.

Postgres still holds what Shopify has no field for: `style`, `department`,
`collection`, size charts, per-colour photos. That is not duplicate product
data.

When Shopify is unreachable the browse path falls back to the local numbers and
logs once; the **order** path refuses (`store_unavailable`) rather than
promising stock it could not check.

**An order is placed or it never happened — there is no third state.** There is
no transaction across the two systems, so `place_order` makes one: Shopify
creates the sale, the local order is written, and the session is **committed
right there**, at the point Shopify accepted it. Before that commit the order
is cancelled on Shopify if anything at all goes wrong, and the tool answers
`order_failed` naming the stage that failed. After it, nothing can take the
order back: a later failure in the turn cannot roll it away, and the staff
alert and the customer's confirmation are bookkeeping that is allowed to fail
loudly without ever being reported as a failed order. Confirming a second time
finds that order (`already_confirmed`) instead of an empty cart.

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

Nothing above `assistant/providers/` imports a vendor SDK; every provider is
called over raw HTTPS. **OpenRouter** (`openrouter.py`) routes the
conversation model by default and runs the whole shop on one model through
one `chat/completions` endpoint: replies, voice-note transcription (an
`input_audio` content part) and photo reading (an `image_url` content part)
all use the same model on the same key -- no separate media model, no second
credential. **Gemini** (`gemini.py`) stays fully configurable as an alternate
provider (`LLM_PROVIDER=gemini`): chat, voice and photos all run on Gemini's
own key in that mode. Swapping providers is one new class and one config
value (`LLM_PROVIDER`), because cost is the reason it may change.

The neutral message format (`assistant/messages.py`) has three shapes and carries
an opaque `signature` blob through the database untouched — that is what makes
a second tool call in the same conversation work on models that demand their
own signatures back. Providers that have no such concept simply ignore it.

### 4. The webhook accepts; the worker answers

`POST /webhooks/whatsapp` verifies, claims the message id in its own committed
transaction, downloads media, and returns 200 — in milliseconds. The agent turn
runs on a worker thread after a short debounce window
(`assistant/dispatcher.py`).

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
| `config/settings.py` | Every setting, read from the environment. No hardcoded credentials. |
| `domain/models.py` | The ORM. `Client`, `Product`, `Variant`, `Order`, `ShippingRate`, `StaffQueueItem`, `WebhookEvent`. |
| `domain/services/` | Business rules. `orders.py` owns "can this order happen?". |
| `domain/services/search_terms.py` | Arabic and franco vocabulary for catalog search. |
| `integrations/` | Outbound clients, one package per vendor: `shopify/`, `whatsapp/`, `instagram/`. |
| `common/security.py` | The shared Meta webhook-signature check both channel adapters use. |
| `api/public_media.py` | `GET /public/media/{token}/...` — the HMAC-gated public URL for catalog files Meta's fetcher has to be able to reach. |
| `integrations/shopify/webhooks.py` | Inbound from Shopify: fulfilments and cancellations. |
| `assistant/runtime.py` | `handle_message()` — the entry point every channel calls. |
| `assistant/dispatcher.py` | Debounce and worker threads. |
| `assistant/agent.py` | The tool-use loop, and the reply sanitisers. |
| `assistant/media.py` | Voice notes and photos. |
| `assistant/prompt.py` | Persona, flow, and the data quirks the model would otherwise get wrong. |
| `assistant/interactive.py` | Tappable pickers, in a channel-neutral shape. |
| `assistant/tools/` | The eighteen tools and their refusals. |
| `assistant/channels/whatsapp.py` | The only WhatsApp-specific code in the conversational path. |
| `assistant/channels/instagram.py` | The Instagram twin: DMs plus comment ingest, same architecture. |
| `assistant/harness/` | Dev-only chat UI. Unauthenticated by design; off unless `HARNESS_ENABLED=1`. |
| `dashboard/` | Staff dashboard: conversations, Shopify (products/orders/customers), statistics, the review queue, settings. See below. |
| `assistant/display.py` | Turning stored history into bubbles a person can read — shared by the harness and the dashboard. |
| `data/` | Catalog metadata Shopify has no field for. Not a product database. |
| `scripts/` | Shopify maintenance. All dry-run by default, idempotent, need `--apply`. |

## A conversation is recorded when it arrives, not when it is answered

The webhook's job is to be fast, so the agent turn runs on a worker thread
after a debounce window. That left a gap nobody designed and everybody paid
for: `sessions` was written by `agent.run_turn`, *after* the model answered,
so between a customer hitting send and the bot replying there was no row at
all — and the dashboard reads `sessions`.

Four different failures therefore looked identical, and all four looked like
nothing:

- the turn is still running (thirty seconds of a model call),
- the conversation is paused for a staff member — who cannot see it in order
  to release it, so it stays paused forever,
- the turn crashed, and `handle_message`'s `session_scope` rolled the whole
  transaction back, message included,
- the message never reached a handler at all (a `reaction`, a template
  `button` tap, a type Meta added later).

From the shop's side each one reads "the bot has never replied to this
number, not even once", with nothing anywhere to say why.

So ingest records it. `assistant/runtime.py::record_inbound` writes the
customer's message to `sessions` in its **own committed transaction**, before
the debounce window opens and before a single model token is spent — it has to
survive whatever the turn does next, including a rollback. It never raises: a
failure to record must not become a failure to answer.

The copy it writes carries `provisional` — the platform message id it was
stored under. When the turn for that message finally runs it stores the real
message (with the photo context and reply-to annotations the model actually
saw) and `session.drop_provisional` removes the copy, keyed on that id. A
provisional message from an *earlier* batch is deliberately left alone: that
row is the only evidence a customer wrote and got nothing back, and the
inbox's `unanswered` filter (`last_role == "customer"`) is built on it.

Two rules follow, and both are load-bearing:

- Nothing on the ingest path may sit between the claim and the record. Blue
  ticks and the typing indicator moved *after* it — a hiccup talking to Meta
  must not be what costs the shop its only copy of what a customer said.
- A message type with no handler is recorded and logged at WARNING with the
  number on it, never dropped in silence. "The bot ignored this number" and
  "the bot never saw anything it could act on" are different problems and
  used to be indistinguishable from both the dashboard and the logs.

## A conversation ends; it is not deleted

`sessions.history` is two things at once: what the model is sent next turn,
and the only record of what the shop and a customer said to each other. Those
have different lifetimes, and conflating them cost a morning of transcripts.

Six hours of silence ends a conversation *for the model*. It used to end it
for everyone: `session.load` overwrote the column with `[]` — and the
dashboard called `load` to **display** a conversation, so opening an old chat
was itself enough to destroy it.

Now the column only ever grows, and `sessions.context_start` marks where the
live conversation begins inside it. Expiry, a staff reset, and messages
scrolling past `HISTORY_CAP` all move that bookmark forward; none of them
remove a message.

- `session.load()` — the live slice. The agent's read; moves the bookmark.
- `session.transcript()` — everything, read-only. What `dashboard/` reads.
- `session.clear()` — soft: ends the conversation, keeps the transcript.
- `session.purge()` — the only function that deletes, wired to no request
  path, for a deletion request from a real person.

`SESSION_ARCHIVE_CAP` (2000) bounds one row so it cannot grow forever, and
logs a warning on the only occasion a message is dropped.

## A second channel: Instagram

Instagram (`assistant/channels/instagram.py` + `integrations/instagram/client.py`)
is a second first-class channel on the same machinery, not a fork of it. The
channel-neutral seams — `runtime.handle_message`, `MessageDispatcher`,
`Pending`, `session.py`, `identities.py`, `interactive.py`'s neutral shapes —
take a channel string, and `"instagram_dm"` is simply another value. Three
things are genuinely Instagram-specific:

1. **The sender registry is per-channel** (`notifications._senders`). An
   unregistered channel falls back to the log and *never* to another
   channel's client — a staff reply to an Instagram conversation delivered
   over WhatsApp, to an IGSID in the phone field, is the wrong-person bug the
   registry exists to make impossible. Order confirmations and status pushes
   go to `(order.source_channel, order.source_external_id)` — the thread the
   order was actually placed in — falling back to the checkout phone over
   WhatsApp only for orders that predate identity-aware delivery.
2. **Outbound images are public URLs.** There is no Instagram upload
   endpoint; Meta fetches what you send. Local catalog files (the size
   charts) therefore go through `api/public_media.py`: an HMAC path
   token under `MEDIA_URL_SECRET`, deterministic per path so Meta's caching
   works, 404 (never 403) on anything wrong, and `data/inbound` unreachable
   through it even under a correctly computed token — customers' own photos
   never become public because the bot needed to send a chart.
3. **Text caps and no lists.** Replies are split client-side at ~950 bytes
   (Arabic is two bytes per character); tappable lists degrade to quick
   replies up to thirteen rows and a numbered plain-text list past that,
   which lands back on `shipping.resolve`'s free-text handling. Templates do
   not exist on this channel; proactive outreach outside a live conversation
   becomes a staff alert by design.

Comments are a surface, not a third channel. A comment webhook event runs a
strict filter chain (disabled → own comment → threaded reply → duplicate →
too old → rate-limited → emoji-only), and what survives gets one fixed public
ack — deliberately **not** a model call — plus one private reply that opens
the DM thread with the session already seeded so the customer's next message
has context. One private reply per comment, ever, enforced by writing
`InstagramCommentReply` before sending: a crash leaves a row that stops a
retry, never a second DM. The shop's own comment is dropped first of all;
answering it is the bot replying to itself publicly, forever. The whole
surface ships with `INSTAGRAM_COMMENTS_ENABLED=0`.

Instagram's long-lived token expires after 60 days with no symptom but auth
errors, so `integrations/instagram/token.py` refreshes it from the
scheduler when it is within ten days of dying, stores the result in
`integration_tokens` (which then wins over the env var), alerts staff if the
refresh fails, and reports the expiry on `/health`.

## The staff dashboard

`request_human` (`assistant/tools/support_tools.py`) has always paused a
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
(`domain/services/auth.py`, `python manage.py create-staff`) with
nowhere to log in. The session is a signed cookie
(`auth.issue_session_token`/`staff_from_session_token`, HMAC over the
standard library) rather than a sessions table — one fewer thing that has to
survive a restart for a shop this size. With no `DASHBOARD_SESSION_SECRET`
configured, login refuses outright (503) rather than signing cookies with a
secret that changes every process restart — the same call
`integrations/shopify/webhooks.py` makes with no webhook secret set.

The dashboard cannot reply into a conversation the bot still owns: `reply`
checks `identities.is_paused` first and refuses (409) otherwise. A person and
the model both writing into the same live turn is exactly the two-writers race
the debounce lock (`assistant/dispatcher.py`) exists to prevent, and the fix is
the same one — exactly one writer at a time, staff included.

### It grew into the whole business's control surface

Conversations was the first section; it is no longer the only one. The same
staff login now also covers:

- **Shopify** (`dashboard/shopify_api.py`) — products (view,
  create, edit), orders (view, fulfil, cancel, edit quantity), customers.
  Full read/write, not the read-only mirror the dashboard started as.
- **Statistics** (`stats_api.py`, `domain/services/dashboard_stats.py`) —
  revenue, orders, AOV, best-sellers, a status breakdown, charted inline
  (hand-rolled SVG, no chart library — the dashboard stays zero-build).
- **The review queue** (`queue_api.py`) — `item_swap` and `alert`, the two
  `StaffQueueItem` kinds that had full backend logic (`orders.apply_swap`,
  `notifications.low_stock_breach` and friends) and no UI at all until this,
  the same gap `handoff` was in before this dashboard existed.
- **Settings** (`settings_api.py`, `domain/services/runtime_flags.py`) —
  the voice/image/interactive-list feature flags, toggleable without a
  redeploy, and a read-only system-status panel.
- **Customers, the WhatsApp side** (`customers_api.py`) — the local
  `Client` table, kept as a *separate* view from Shopify's customers rather
  than merged, for the same reason stats reads Shopify directly: a customer
  who only ever ordered on the website has no `Client` row, and pretending
  otherwise would double- or under-count.

Two decisions shape all of it. First: **Shopify orders and stock still win**.
Store-wide reads (orders, customers, stats) go straight to Shopify — see
`integrations/shopify/admin_orders.py` / `admin_customers.py` / `domain/services/dashboard_stats.py`
— because the local `orders` table only ever holds what the *bot* sold; a
number built from Postgres alone would silently miss every website sale.
Second: **write actions prefer the existing order service when a local row
exists**. Cancelling or editing an order the bot placed goes through
`domain/services/orders.py`'s `cancel()` / `modify_quantity()` — already
transactional, already notifies the customer — rather than calling
`shopify_orders` a second, divergent way; only a pure website order (no local
row) talks to Shopify directly from the dashboard route.

Product create/edit (`integrations/shopify/admin_products.py`) pushes to Shopify first,
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
