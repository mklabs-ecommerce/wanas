# Wanas Gallery — Phase 1 build instructions

> **Status note:** this is the original Phase 1 build brief, kept because the business rules in it (variant math, per-colour pricing, session/tool-loop limits, the seventeen tools) are still exactly how the chatbot behaves today. Two things it describes have since changed: the **custom storefront** (item 21 below, `docs/components/03-website.md`) and the **admin dashboard** (item 4 and 15 below, `docs/components/12-admin-dashboard.md`) were both removed from this repository — the store moved to **Shopify** (theme + checkout + Admin), which is now also the source of truth for orders, inventory, and live price. See the root `README.md` and `CLAUDE.md` for the current architecture. Everything else below — database, backend, WhatsApp chatbot — is still accurate.

This is a B2C clothing e-commerce platform. Full system design lives in `/docs/components/` — read the relevant file(s) before building each piece listed below. This file defines what's **in scope right now** and what isn't, so nothing gets built ahead of what's actually being tested.

## Phase 1 scope

**Build these, in this order:**

1. **Database** — Client DB, Product DB, Order DB, Order Feedback (`docs/components/07`–`10`) **plus the supporting tables in `16-supporting-tables.md`**: `channel_identities`, `sessions`, `cart_items`, `shipping_rates`, `staff`, `audit_log`, `webhook_events`. Those seven are as much in scope as the four above — items 2–4 below don't work without them.
   - Seed the Product DB from `data/products_seed.json` — **18 products with 208 variants (114 in stock)**. Product photos live in `data/images/<source_handle>/`; the handle is a meaningless Shopify leftover, so **always read paths from the `images` and `color_images` fields**, never construct them.
   - `data/merge_catalog.py` produces the **initial seed only**. It is not an incremental sync — re-running it after launch rebuilds the catalog from the scrape. It does carry stock and thresholds forward by `variant_id`, but treat a re-run as a deliberate act. The older `data/import_catalog.py` is superseded; don't run it.
2. **Backend** — Order, Inventory, and Notification services as described in `docs/components/01-backend-platform.md`. Build it as one application with internal modules (a modular monolith), not separate services — see that file for why.
3. **One chatbot channel: WhatsApp only.** The chatbot is an **LLM tool-use agent**, not a keyword classifier — read `docs/components/02-chatbot.md` for the architecture and `15-tool-contracts.md` for the exact arguments and return shapes of all seventeen tools, then `04-whatsapp-channel.md` for the platform integration. Do **not** build Facebook DM, Instagram DM, TikTok DM, or public comment auto-reply yet.
   - **Build a local chat harness before wiring WhatsApp** — a terminal or single-page interface that calls the same `handle_message(channel, external_id, text)` entry point with a fake identity. WhatsApp needs Meta approval and a live webhook; without a local way in, the hardest component in the system is also the last one you can test. The harness is throwaway scaffolding for the flow, not a product surface, and the WhatsApp adapter should be the only thing that changes when it's swapped in.
4. ~~**Admin dashboard**~~ — **removed.** A staff dashboard reading the local database was built for Phase 1 (per `docs/components/12-admin-dashboard.md`) but has since been removed from this repository as part of the Shopify migration; order/inventory/product management now happens in Shopify Admin. `request_human` still writes a handoff record and pauses the conversation — there is currently no UI to resolve one, which is a known gap, not an oversight.

## Explicitly out of scope for Phase 1

Don't build these yet — they depend on things not in place, or weren't part of what's being tested right now:

- ~~**Website**~~ — a custom storefront was later built for this project, then removed in favour of a Shopify theme + checkout. See `CLAUDE.md`.
- **Facebook DM, Instagram DM, TikTok DM, public comment auto-reply** — blocked on Meta/TikTok business verification; WhatsApp can be tested now via Meta's test-recipient-number mode without full verification (see `docs/components/04-whatsapp-channel.md` and the operational notes on Meta review lead times).
- **Discount codes** (`11-discount-database.md`) — not part of this test's necessary features.
- **Image recognition** (`14-image-recognition.md`) — not part of this test's necessary features. **But incoming photos still need a Phase 1 answer:** an image goes straight to human handoff with the photo attached, same queue as an unclear conversation. Don't give the model an image tool and don't let it describe the photo — a guess about a garment the shop may not make is worse than a handoff.
- **Analytics** (`13-analytics.md`) — needs real order volume to be meaningful; comes after this phase.

## Data assumptions in this phase (change these here, not ad hoc elsewhere)

- **Products vs variants:** a product is what the customer talks about; a **variant** is what they buy. Stock, price and `low_stock_threshold` live on the variant. `variant_id` is the only thing that can go in a cart — there is no "product + chosen options" path anywhere. The `sizes` / `colors` / `lengths` lists on a product are display summaries only; never make an availability decision from them.
  - **Why:** per-axis availability lists would say the Cairokee T-shirt comes in XL/Brown, because XL exists (in Black) and Brown exists (in S/M/L). It doesn't. **Every axis combination has a row — 94 of the 208 are at `stock_qty: 0`** — and the count alone decides buyability. A row existing never means it's for sale.
- **`price` on a product is not a quotable number.** It's the *lowest* variant price, and `original_price` is the highest *pre-discount* price — the two are not a range of what anyone pays. Three products cost more in one colour: the WANAS Hoodie is 650 in black and olive but **700 in grey**. Only a *variant* price may ever be said to a customer; for a product, quote `min` and `max` of the variants' current prices ("من 650") — which is what `get_products` returns as `price_from` / `price_to`.
- **Colour is a variant axis, never a product.** The source store split every colourway into its own product; the seed merges them, so **43 source products become 18** with all 208 variants intact. One `WANAS Hoodie` in three colours, not three hoodies. The merge is an explicit map in `merge_catalog.py` — don't regenerate it from names, and don't reintroduce colour into a product name.
- **Taxonomy:** six `category` values (`T-Shirts`, `Hoodies & Sweatshirts`, `Polo Shirts`, `Joggers & Sweatpants`, `Jackets`, `Tops`), plus `style` as a filter axis and `department` (`unisex` / `women`). Crewnecks and quarter-zips are **not** categories — they're `style` values under Hoodies & Sweatshirts. See `08-product-database.md`.
- **Collections are optional and separate.** Only `WINTER COLLECTION` (7) and `CAIROKEE MERCH` (3); the other 8 products have `collection: null`, which is correct, not missing data. Nothing may require a collection to find a product.
- **Stock:** every in-stock variant starts at **10 units**, every out-of-stock one at **0**. That's 114 of 208. Only the Cairokee Hoodie is sold out across every variant.
- **Low stock threshold:** 2 units on every variant, adjustable from the dashboard.
- **Shipping:** a flat fee **per governorate**, from a small rate table (`governorate` → `fee`) that staff edit from the dashboard. The bot asks which governorate as part of collecting the address — it's a picked value, not free text, because it sets the price. The fee is added to the total and **copied onto the order**, so a later rate change never alters a past order.
  - **Seed from `data/governorates.json`** — all 27, keys English and stable, Arabic labels for display, `fee: null` throughout. Match customer input against both spellings; never let a governorate be free text (see `16-supporting-tables.md`). The real numbers come from the shop, and an order for a governorate with no fee set must be refused.
- **Email is optional in Phase 1.** Every order arrives over WhatsApp and the flow never asks for an email, so `clients.email` is nullable and **the customer confirmation goes out on WhatsApp only**. Staff alerts land in the dashboard's alert inbox. This deliberately removes the email provider from Phase 1's dependencies — wire one up when the website ships and email is actually collected.
- **Order totals are four separate numbers** — `subtotal`, `discount_amount`, `shipping_fee`, `total`. Don't collapse them: shipping isn't revenue, and analytics needs each one on its own (see `09-order-database.md`).
- **Payment:** cash on delivery only for this phase. Card/wallet payment (and the `Pending payment` status/timeout logic in `01-backend-platform.md`) can wait until a real payment gateway is wired up — don't build against a placeholder gateway.
- **Colour:** every product now has a colour choice, so colour is a required pick at order time alongside size. Ask for it the same way.
- **Length:** the Worker Jacket also carries a `length` (`Long` / `Short`) — a third axis on that product only, so its variants are size × colour × length.
- **Pricing:** `price` is what the customer pays, `original_price` the pre-discount price, `on_sale` true when they differ — all per variant. **14 of the 18 products have a discounted variant**, so order summaries and product replies should show both numbers.
- **Size charts:** `data/size_charts.json` + the images in `data/size-charts/`. **12 charts covering all 18 products.** When a customer asks about sizing the bot returns the numbers *and* sends the chart image. **It must never estimate a measurement or reuse another product's chart** — sizing wrong doesn't confuse a customer, it causes a return.
  - Charts are assigned **per product in the merge map**, never derived from `category`: `T-Shirts` and `Polo Shirts` each span two charts, and `Hoodies & Sweatshirts` spans five.
  - Numbers are **garment-flat, not body measurements** — say which, every time.
  - **Two conditional charts:** `worker-jacket` has separate short- and long-sleeve rows (ask which length first), and `wns-tops` has **no XL** (those two Tops are S/M/L only).
  - `size_chart` is nullable and the "no chart" path must still be built — new products will arrive before their charts do.

## The chatbot is an LLM agent — the decision and its constraint

The bot uses an LLM with **tool calling**. The model handles understanding and phrasing; it has no access to the catalog, cart, or orders except through a fixed set of tools, and **every fact in every reply comes from a tool result**. The model must never state a price, a size, or an availability from its own knowledge.

That constraint is enforced by the tools, not by the prompt. A prompt instruction is a preference; a tool that refuses is a guarantee. In particular: a `variant_id` cannot be guessed, so the model must call `get_variants` before it can add anything to a cart, and `confirm_order` re-checks stock itself rather than trusting the conversation.

**Provider: Gemini Flash to start, behind a provider abstraction.** Cost is the reason it may change, so nothing above that layer may import a vendor SDK, and swapping providers must mean writing one class and changing one config value. Treat this as a hard architectural boundary, not a nice-to-have.

Architecture, guardrails, session handling and failure behaviour are in `docs/components/02-chatbot.md`. **The seventeen tools' exact arguments and return shapes are in `15-tool-contracts.md`** — build against that, not against the summary table. Read both before writing any chatbot code.

**Numbers that are decided, so nobody has to guess:** history cap 40 messages, session expiry 6 hours, tool-loop cap 8 turns, max 10 units per cart line, order IDs `WNS-<n>` starting at 1001.

## Tech stack (decided — don't re-litigate)

**Python 3.11+, FastAPI, PostgreSQL.**

- **Python** because the LLM provider SDKs are most mature there, and because the working prototype this design came from is Python — its provider-abstraction and session-trimming logic is worth reading before rewriting.
- **FastAPI** because the WhatsApp webhook is an HTTP endpoint; its request/response models line up with the tool contracts in `15-tool-contracts.md`.
- **PostgreSQL** because the order transaction in `01-backend-platform.md` needs real transactional guarantees and the atomic conditional decrement (`UPDATE ... SET stock_qty = stock_qty - n WHERE stock_qty >= n`). SQLite is fine for local development, but the schema must not depend on anything SQLite-specific.
- **Sessions and carts live in Postgres, not Redis.** At this volume Redis is an extra moving part for no gain, and both need to survive a restart anyway.
- **Shopify** is the source of truth for orders, live inventory, and live price (see `CLAUDE.md`). Postgres still holds catalog metadata Shopify has no field for (style, department, collection, size charts, colour photos), sessions/chat history, shipping rates, staff, and the audit log/queues.

## What to test

Not full coverage — the parts where a silent bug is expensive:

- **The atomic order transaction.** Two concurrent orders for the last unit: exactly one succeeds, and a failure mid-transaction leaves no stock decremented and no order written.
- **Every tool refusal in `15-tool-contracts.md`.** These are the guardrails; each one needs a test proving it refuses, because the whole design rests on tools refusing rather than the model behaving.
- **Session trimming.** History over the cap trims to a user message and never splits a tool-call/tool-result pair.
- **The seed import.** 18 products, 208 variants, 114 in stock after loading — the same assertions `merge_catalog.py` makes.

## Folder structure

Current, post-Shopify-migration layout — see `CLAUDE.md` for the full picture:

```
/docs/components/   an index + specs (read the relevant one before building each piece)
                    15 = tool contracts, 16 = supporting tables — both are normative
/data/               products_seed.json + size_charts.json + governorates.json
                     + merge_catalog.py
                     + images/ (product photos) + size-charts/ (chart images)
                     -- catalog metadata Shopify has no field for; not a duplicate
                        product database, price/stock come from Shopify live.
/backend/            Order, Inventory, Notification services + database models
                     + Shopify integration (backend/integrations/, backend/services/shopify_*.py)
/chatbot/            the agent loop, the tools, the provider layer, session storage
```

No `/dashboard/`, `/storefront/`, or `/web/` — removed with the Shopify migration.

## Testing this phase

WhatsApp can be tested live, right now, without full Meta Business Verification: create a Meta app, add the WhatsApp product, use the free test phone number, and add your own and your teammate's numbers as verified test recipients. That gets real API calls end-to-end — order placement, status tracking, modification, feedback — without needing the app to be publicly approved yet.

**Fill these in before testing** — the specs are complete but these values aren't, and nothing works without them:

- Meta app credentials + the verified test recipient numbers
- The LLM API key and model name
- The shipping fees in `data/governorates.json` (all 27 seeded, every fee blank)
- At least one staff login for the dashboard

No email provider is needed — see the email note above.

**Definition of done for this phase:** a test WhatsApp number can place a real order (checked against seed stock), the order shows up correctly in the dashboard, stock decrements correctly, a status change pushes a WhatsApp update, and a modification/cancellation before "Shipped" applies automatically per the rules in `02-chatbot.md`.
