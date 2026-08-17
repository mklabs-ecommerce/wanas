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
  picked value, not free text, because it sets the price. **An order for a
  governorate with no fee set must be refused.** The fee is copied onto the
  order at confirm time, so a later rate change never alters a past order.
- **Order totals are four separate numbers** — `subtotal`,
  `discount_amount`, `shipping_fee`, `total`. Don't collapse them.
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

**Provider: Gemini, behind a provider abstraction
(`chatbot/providers/`).** Cost or availability is the reason it may change,
so nothing above that layer may import a vendor SDK, and swapping providers
must mean writing one class and changing one config value. Treat this as a
hard architectural boundary, not a nice-to-have.

Tool argument/return shapes and refusal codes are defined by the `@tool`
decorators in `chatbot/tools/*.py` and pinned down by
`tests/test_tool_contracts.py` (every refusal, and that there are exactly
seventeen tools) — that pairing is what to build against, not a separate
spec.

**Numbers that are decided, so nobody has to guess:** history cap 40
messages, session expiry 6 hours, tool-loop cap 8 turns, max 10 units per
cart line.

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
- **The seed import.** Product/variant counts match `merge_catalog.py`'s own
  assertions after loading.
- **No double-decrement.** An order writes stock down on Shopify exactly
  once; a cancellation restocks exactly once.

## Known gap

`request_human` pauses a conversation and writes a handoff record, but
there is currently no staff UI to resolve one (the dashboard that used to
do this was removed — see `CLAUDE.md`). Don't silently "fix" this by
rebuilding a dashboard; it's a deliberate, documented gap.
