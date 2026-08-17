# Component deep-dive index

Each file below covers one component in full detail: what it does, exactly how it does it, its data, and how it interacts with everything else. Review them independently — leave comments/questions per file and I'll revise just that one.

| # | File | Covers |
|---|---|---|
| 01 | `01-backend-platform.md` | Order, inventory, payment & notification services; concurrency; webhooks |
| 02 | `02-chatbot.md` | LLM tool-use agent: the loop, the tools, guardrails, session state |
| 04 | `04-whatsapp-channel.md` | Ordering, tracking, confirmations, platform specifics |
| 05 | `05-facebook-instagram.md` | DM ordering/support + public comment auto-reply |
| 06 | `06-tiktok-dm.md` | DM ordering/support and its platform constraints |
| 07 | `07-client-database.md` | Client DB schema and behavior |
| 08 | `08-product-database.md` | Product DB schema and behavior |
| 09 | `09-order-database.md` | Order DB schema and behavior |
| 10 | `10-order-feedback.md` | Order Feedback schema and behavior |
| 11 | `11-discount-database.md` | Discount DB schema and behavior |
| 13 | `13-analytics.md` | Metrics, their data sources, and computation |
| 14 | `14-image-recognition.md` | Matching customer photos to the catalog, and routing custom requests to staff |
| 15 | `15-tool-contracts.md` | **Normative.** Exact arguments and return shapes for all seventeen agent tools |
| 16 | `16-supporting-tables.md` | **Normative.** Sessions, carts, channel identities, shipping rates, staff, audit log, webhook idempotency |

**Adjustments folded in throughout, most recent first:**
- The custom storefront (`03-website.md`) and the staff dashboard (`12-admin-dashboard.md`) were removed from this repository — the store moved to Shopify (theme + checkout), and Shopify Admin is now where orders/inventory/products are managed. See the root `README.md` and `CLAUDE.md` for the current architecture. The rest of the specs below (backend platform, chatbot, channels, databases, tool contracts) still describe the live system.
- Two normative specs added: **`15-tool-contracts.md`** (the agent's tools grew to seventeen — feedback, human handoff and identity linking were described in prose but had no tool) and **`16-supporting-tables.md`** (seven Phase 1 tables that had no home).
- **Email is optional in Phase 1.** The WhatsApp flow never collects one, so the customer confirmation is WhatsApp-only and staff alerts go to the dashboard inbox — no email provider needed to ship.
- **Colour is now a variant axis, not a product.** 43 source products merged into **18**, all 208 variants intact. Taxonomy rebuilt on an ASOS-style model: six `category` values, `style` as a filter, `department`, and collections reduced to two optional ones — see `08-product-database.md`.
- **Size charts** are now catalog data (`data/size_charts.json`) — 12 charts covering all 18 products, mapped product by product because cut doesn't follow category. The bot answers sizing questions with the numbers *and* the chart image, which gives replies an **attachment** channel for the first time — see `08-product-database.md`, `02-chatbot.md`, `04-whatsapp-channel.md`.
- Shipping is a flat fee **per governorate**, and order totals are now four separate numbers (`subtotal` / `discount_amount` / `shipping_fee` / `total`) — see `09-order-database.md`.
- WhatsApp goes **direct to Meta's Cloud API** in Phase 1, not through a BSP — that's what makes the free test number work — see `04-whatsapp-channel.md`.
- The dashboard has **staff login, one role, every action attributed** — see `12-admin-dashboard.md`.
- **The chatbot is now an LLM tool-use agent, not a keyword classifier.** The model understands and phrases; the tools hold every fact and every rule. Provider is swappable by design — see `02-chatbot.md`.
- **Stock moved from the product to the variant** (18 products / 208 variants). Per-axis availability lists are gone: they implied combinations the shop doesn't stock — see `08-product-database.md`.
- The product catalog is now seeded from the live store scrape (177 local photos) instead of a spreadsheet. This added per-variant availability across three axes (size, colour, length) and sale pricing on most of the catalog — see `08-product-database.md`.
- Order items now record the chosen size/colour/length and the price paid, and orders carry their own copied shipping address — see `09-order-database.md`.
- Guest checkout now confirms a phone/email match with the customer before linking them to an existing client record, instead of merging silently — the same rule the chatbot already used for cross-channel identity. See `07-client-database.md`.
- The post-delivery feedback request always goes out on WhatsApp, not "whichever channel the customer used" (TikTok can't be messaged first) — see `10-order-feedback.md`.
- Shipping details are now confirmed (not silently skipped) on every order, even for known customers — see `02-chatbot.md`.
- A shipped order that can't be modified now gets offered as a **new order** if the customer wants more/different items, and is only routed to human support for genuine problems (wrong/damaged item) — see `02-chatbot.md`.
- Chatbot language handling now reflects natural Arabic/English code-switching (Arabic base with product names and other terms kept in English) instead of replying purely in one detected language — see `02-chatbot.md`.
- Customer-submitted product photos are matched against the catalog automatically; anything that doesn't match confidently is routed to staff to judge production feasibility, not decided by the bot — see `14-image-recognition.md`.
- ~~Once an order reaches Confirmed status, the customer receives both a confirmation email and a WhatsApp message.~~ Superseded: no email is collected in Phase 1, so WhatsApp is the confirmation — see the email entry above.
