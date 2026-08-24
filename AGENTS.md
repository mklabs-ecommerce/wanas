# AGENTS.md — business rules and data assumptions

These are the rules the chatbot is built against. They're not derivable from
skimming the code, so change them here, deliberately, rather than ad hoc in a
service file. For architecture and where things live, see `CLAUDE.md`.

## Catalog and variants

- **Products vs variants:** a product is what the customer talks about; a
  **variant** is what they buy. Stock, price and `low_stock_threshold` live
  on the variant. `variant_id` is the only thing that can go in a cart —
  there is no "product + chosen options" path anywhere. The `sizes` /
  `colors` / `lengths` lists on a product are display summaries only; never
  make an availability decision from them.
  - **Why:** per-axis availability lists would say the Cairokee T-shirt
    comes in XL/Brown, because XL exists (in Black) and Brown exists (in
    S/M/L). It doesn't. **Every axis combination has a row** — a row
    existing never means it's for sale; the stock count alone decides
    buyability.
- **`price` on a product is not a quotable number.** It's the *lowest*
  variant price, and `original_price` is the highest *pre-discount* price —
  the two are not a range of what anyone pays. Three products cost more in
  one colour: the WANAS Hoodie is 650 in black and olive but **700 in
  grey**. Only a *variant* price may ever be said to a customer; for a
  product, quote `min` and `max` of the variants' current prices — which is
  what `get_products` returns as `price_from` / `price_to`.
- **Colour is a variant axis, never a product.** One `WANAS Hoodie` in three
  colours, not three hoodies. The merge from the original per-colour source
  products is an explicit map in `data/merge_catalog.py` — don't regenerate
  it from names, and don't reintroduce colour into a product name.
- **Taxonomy:** six `category` values (`T-Shirts`, `Hoodies & Sweatshirts`,
  `Polo Shirts`, `Joggers & Sweatpants`, `Jackets`, `Tops`), plus `style` as
  a filter axis and `department` (`unisex` / `women`). Crewnecks and
  quarter-zips are **not** categories — they're `style` values under
  Hoodies & Sweatshirts.
- **Collections are optional and separate.** Only `WINTER COLLECTION` and
  `CAIROKEE MERCH`; most products have `collection: null`, which is correct,
  not missing data. Nothing may require a collection to find a product.
- **Length:** the Worker Jacket also carries a `length` (`Long` / `Short`) —
  a third axis on that product only, so its variants are size × colour ×
  length. Every other product's variants are size × colour.
- **Low stock threshold:** 2 units per variant by default.

## Shopify is the source of truth for orders, live inventory, and live price

- When the bot sells something it creates a real Shopify order
  (`orderCreate`); **Shopify decrements inventory**, not this codebase.
  There is deliberately no manual `inventory.decrement` in the order path —
  Shopify already does it, and doing it twice would silently oversell.
- Price and stock are read **live from Shopify** at message time
  (`backend/services/shopify_catalog.py`), matched to the local catalog row
  by SKU (`variant_id`). If Shopify is unreachable, the bot falls back to
  the local database's numbers and logs a warning once — never fails the
  conversation.
- PostgreSQL still legitimately holds catalog fields Shopify has no place
  for: `style`, `department`, `collection`, size charts, per-colour photos.
  This is not duplicate product data — don't move price/stock into Postgres
  "for convenience," and don't treat the local catalog fields above as
  something to migrate away.

## Shipping and orders

- Shipping is a flat fee **per governorate**, from a rate table
  (`ShippingRate`, seeded blank from `data/governorates.json`, 27 entries).
  The bot asks which governorate as part of collecting the address — it's a
  picked value, not free text, because it sets the price. That is now literal
  rather than aspirational: `ask_governorate` sends a tappable WhatsApp list,
  in **two steps** (region, then governorate) because Meta allows ten rows per
  list and there are twenty-seven. The regions live in
  `backend/services/shipping.py`; every governorate belongs to exactly one, and
  a test asserts both that and the ten-row ceiling. A customer who simply names
  their governorate skips the picker entirely. **An order for a governorate
  with no fee set must be refused.** The fee is copied onto the
  order at confirm time, so a later rate change never alters a past order.
- **Order totals are four separate numbers** — `subtotal`,
  `discount_amount`, `shipping_fee`, `total`. Don't collapse them.
- **Status is Shopify's to report, not ours to assume.** Staff fulfil in
  Shopify Admin; `backend/webhooks/shopify.py` turns that into
  `orders.advance_status`, which is what sends the packed / shipped / delivered
  messages and the feedback request. Statuses move **forward, one stage at a
  time** — and because those messages are only sent after the transaction
  commits, the stage has to be bound when the transition happens, not read off
  the order later. A single fulfilment walks two stages in one transaction, and
  reading late made both of them say "shipped".
- **Payment is cash on delivery only.** No gateway, no `Pending payment`
  status/timeout logic.
- **Email is optional.** Orders arrive over WhatsApp and the flow never asks
  for an email, so `clients.email` is nullable and the customer confirmation
  goes out on WhatsApp only (Shopify's own order-confirmation email is
  suppressed — `sendReceipt: false` — since the bot already sent one).

## Pricing and sizing

- **Pricing:** `price` is what the customer pays, `original_price` the
  pre-discount price, `on_sale` true when they differ — all per variant.
  Order summaries and product replies should show both numbers when they
  differ.
- **Size charts:** `data/size_charts.json` + the images in
  `data/size-charts/`. When a customer asks about sizing the bot returns the
  numbers *and* sends the chart image. **It must never estimate a
  measurement or reuse another product's chart** — sizing wrong doesn't
  confuse a customer, it causes a return.
  - Charts are assigned **per product**, never derived from `category`.
  - Numbers are **garment-flat, not body measurements** — say which, every
    time.
  - The Worker Jacket has separate short- and long-sleeve chart rows (ask
    which length first); some products have no XL row. Don't assume a fixed
    row set.
  - `size_chart` is nullable and the "no chart" path must still work — new
    products can arrive before their chart does.

## The chatbot is an LLM agent — the decision and its constraint

The bot uses an LLM with **tool calling**. The model handles understanding
and phrasing; it has no access to the catalog, cart, or orders except
through a fixed set of tools, and **every fact in every reply comes from a
tool result**. The model must never state a price, a size, or an
availability from its own knowledge.

That constraint is enforced by the tools, not by the prompt. A prompt
instruction is a preference; a tool that refuses is a guarantee. In
particular: a `variant_id` cannot be guessed, so the model must call
`get_variants` before it can add anything to a cart, and `confirm_order`
re-checks stock (against Shopify) itself rather than trusting the
conversation.

**Providers sit behind a provider abstraction (`chatbot/providers/`); the
default is OpenRouter (`chatbot/providers/openrouter.py`), called over raw
HTTPS for chat, voice-note transcription and photos alike -- all three run on
the same model ("google/gemini-3.1-flash-lite" unless `LLM_MODEL` says
otherwise) through the same `chat/completions` call, keyed by
`OPENROUTER_API_KEY` alone. Gemini
(`chatbot/providers/gemini.py`) is kept as a fully configurable alternate
provider (`LLM_PROVIDER=gemini`) that handles chat, voice and photos itself
on its own key.** Cost or availability is the reason the provider may change,
so nothing above that layer may import a vendor SDK, and swapping providers
must mean writing one class and changing one config value. Treat this as a
hard architectural boundary, not a nice-to-have.

Tool argument/return shapes and refusal codes are defined by the `@tool`
decorators in `chatbot/tools/*.py` and pinned down by
`tests/test_tool_contracts.py` (every refusal, and that there are exactly
eighteen tools) — that pairing is what to build against, not a separate
spec.

**A photo and a voice note do not escape any of this.** A voice note is
transcribed into ordinary text before the agent sees it. A photo is read
against a shortlist built from the real catalog and handed to the agent as a
*note* — carrying the product's **name**, never its id — that says, in as many
words, to verify with the tools before quoting anything. A `product_id` the
shop does not have is discarded before the caller ever sees it, and every
failure in either path falls back to the human handoff, which is what happened
to all of them before. See `docs/MEDIA.md`.

**Numbers that are decided, so nobody has to guess:** history cap 40
messages, session expiry 6 hours, tool-loop cap 8 turns, max 10 units per
cart line, inbound debounce 6 seconds, image-match confidence 0.6.

**The catalog is in English; the customers are not.** Search goes through
`backend/services/search_terms.py`, which folds Arabic spelling variants, maps
Arabic and franco words onto the English the catalog actually uses, and drops
the padding a spoken request carries. This is a rule below the model, not a
habit the model has: before it existed `get_products(query="هودي أسود")`
returned nothing, and the bot only worked because Gemini happened to translate
first. Adding a word is one line in that file.

## The Instagram surface

Instagram (`"instagram_dm"`, `chatbot/channels/instagram.py` +
`backend/integrations/instagram_client.py`) is a second first-class channel
on the same agent, tools, carts, orders and dashboard. Its platform limits,
all enforced below the model:

- **≈1000-byte text cap** — Arabic is two bytes per character in UTF-8, so
  the real limit is ~500 characters. The client splits long replies at
  paragraph/sentence boundaries; the model is never told about it.
- **No tappable lists** — quick replies only, max **13**, titles max **20
  characters** (truncated by the client). The 27-governorate picker degrades
  to a numbered plain-text list, which lands on `shipping.resolve`'s free-text
  handling — no special inbound parsing exists or is needed.
- **No templates** — proactive outreach outside a live conversation becomes a
  staff alert by design (`error="instagram_has_no_templates"`); there is no
  approved-template escape hatch to build against.
- **24-hour messaging window** for free-form replies from staff; outside it a
  send fails visibly (dashboard shows the warning).
- **Images are public URLs, never uploads** — Meta fetches them itself;
  local catalog files go through the HMAC-token route in
  `backend/public_media.py`. `data/inbound` (customers' own photos/voice
  notes) is never servable through that route, token or no token.
- **Comments are a public surface, shipped OFF**
  (`INSTAGRAM_COMMENTS_ENABLED=0`). One fixed public ack + one private reply
  per comment, ever (written to `instagram_comment_replies` *before* the
  send), inside Meta's **7-day private-reply window**; the shop's own
  comments are dropped before anything else runs. The agent never runs on
  comment text.
- The long-lived token expires after **60 days**;
  `backend/services/instagram_token.py` refreshes it automatically and alerts
  staff when it cannot.

## What to test

Not full coverage — the parts where a silent bug is expensive:

- **The atomic order transaction.** Two concurrent orders for the last
  unit: exactly one succeeds, and a failure mid-transaction leaves no stock
  decremented and no order written.
- **Every tool refusal.** These are the guardrails; each one needs a test
  proving it refuses, because the whole design rests on tools refusing
  rather than the model behaving.
- **Session trimming.** History over the cap trims to a user message and
  never splits a tool-call/tool-result pair.
- **The media fallbacks.** Every way a voice note or a photo can fail still
  reaches a person. That path used to be the *only* path, so it is the one that
  must not quietly stop working.
- **Webhook signatures and idempotency**, on both sides. A retry must not send
  a second "your order shipped" or place a second order.
- **The seed import.** Product/variant counts match `merge_catalog.py`'s own
  assertions after loading.
- **No double-decrement.** An order writes stock down on Shopify exactly
  once; a cancellation restocks exactly once.

## The handoff has a UI now

`request_human` pauses a conversation and writes a handoff record.
`dashboard/` (a staff login, `/dashboard`) lists what is waiting,
lets staff reply — which un-pauses the conversation and resolves the record
in the same transaction — or resolve it without a reply. See "The staff
dashboard" in `docs/ARCHITECTURE.md`.
