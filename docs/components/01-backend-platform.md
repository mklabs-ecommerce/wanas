# 01 — Backend platform

## Purpose

The single system every channel talks to. No channel — website, WhatsApp, FB DM, IG DM, TikTok DM — ever reads or writes the databases directly. This is what guarantees stock, prices, and order state stay identical no matter which channel a customer used.

## Internal architecture

Described earlier as five "services," but that doesn't mean five separately deployed applications. At this scale (500+ orders/month), running five independent services would be more operational overhead than the problem needs — separate deployments, separate monitoring, network calls between them for things that could be a function call.

**Recommendation: build it as one deployed application with five internal modules** (a "modular monolith"): Order, Inventory, Payment, Chatbot Orchestrator, Notification. Each module owns its own responsibility and its own slice of the database, and they call each other directly in-process. This gets the clean separation of concerns without the infrastructure cost. Splitting into real microservices only starts to pay off at meaningfully larger scale or team size than this — worth revisiting later, not now.

**One deployed app, but `/backend/` and `/chatbot/` are separate top-level folders** (see `AGENTS.md`). The chatbot module is large enough — agent loop, provider layer, tools, session storage — that burying it inside `/backend/` would hide its internal structure. It is still imported and run in the same process, not deployed separately. The rule that matters is the dependency direction: `/chatbot/` calls into `/backend/`, never the reverse.

## Order service

**What it does:** the only place "can this order happen?" gets decided.

**How it works, step by step:**
1. Receives a cart, the customer's identity, shipping details, payment method, and an optional discount code — from either the website directly, or from the Chatbot Orchestrator on behalf of a chat channel. **Every cart line is a `variant_id` plus a quantity** — not a product plus a set of chosen options. The variant already encodes size, colour and length, so a line that resolves to a real variant is by definition a buyable combination; there is no separate validation step that can be forgotten (see `08-product-database.md`).
2. Re-checks live stock for every line (a second check — the first happened while the customer was browsing; this one happens right before committing, since time has passed and someone else may have bought the last unit). One check, against one number: the variant's `stock_qty`.
3. If a discount code was given, validates it (see `11-discount-database.md`) and calculates the discounted total. *Phase 1: discount codes are out of scope, so this step is a no-op and `discount_amount` is always 0 — see `AGENTS.md`.*
4. Runs everything below as **one atomic database transaction** — either all of it commits, or none of it does:
   - Create/update the Client DB record
   - Decrement Product DB stock for each item
   - Create the Order DB record with `source_channel` set
   - Increment the Discount DB `times_used` counter, if a code was used *(Phase 2)*
5. Branches on payment method:
   - **Cash on delivery** → status set directly to `Confirmed`
   - **Card/wallet** → status set to `Pending payment`, a payment link (or, for TikTok, an order code) is generated, and a 30-minute timer starts
6. Publishes an `order_confirmed` event once status reaches `Confirmed` (immediately for COD, or once payment lands for card/wallet) — this is what the Notification service listens for to fire the staff alert and the customer's WhatsApp confirmation. (A customer email joins them once an email is actually collected — see `AGENTS.md`.)

**Why the transaction matters:** if stock gets decremented but the order record fails to save (a crash, a network blip), your inventory count is now permanently wrong with nothing to show for it. Treating the whole set of writes as one transaction means a failure rolls everything back — the customer sees a clear "please try again" instead of a silently broken order.

## Inventory service

**What it does:** owns `stock_qty` and `low_stock_threshold` **per variant** — the single source of truth every other service checks against. 208 variants, 114 currently in stock.

**Stock lives on the variant, not the product.** There is exactly one number per buyable thing, so there's no second availability flag that can drift out of sync with it, and no way for the catalog to imply a combination it doesn't stock (see `08-product-database.md`). "Sold out" at the product level is derived — every variant at zero — not stored.

**How it works:**
- Stock changes are never "read the number, check it, then write a new number" in application code — that has a race condition (two requests can both read "1 left" before either writes). Instead, the decrement is a **single atomic database operation**: "reduce stock by this amount, but only if enough stock exists." If two orders hit at the same instant, the database itself guarantees only one succeeds; the other is told the item just sold out.
- **Reservation for pending payment:** when an order enters `Pending payment`, its stock is already decremented — not just marked reserved, genuinely subtracted — so it can't be double-sold. If the 30-minute window expires without payment, a scheduled job cancels the order and adds the stock back.
- **Threshold events:** every decrement checks the resulting `stock_qty` against `low_stock_threshold`. Crossing it publishes a `low_stock_breach` event for the Notification service.

## Payment service

> **Not built in Phase 1** — cash on delivery only, so there is no gateway, no payment link, no `Pending payment` status and no timeout job. Don't build against a placeholder gateway. The rest of this section describes the component once a real gateway is wired up.

**What it does:** handles the money side, without the Order service needing to know payment-provider details.

**How it works:**
- Card/wallet: generates a hosted payment link through the payment gateway, tied to the order ID. Listens for the gateway's confirmation webhook to mark payment received.
- Cash on delivery: no online step — payment is confirmed on delivery, handled operationally rather than by this service.
- TikTok: since DM links aren't clickable, generates a short order code instead. The customer enters it on the website, which pulls up the pre-built order for payment. The code expires on the same 30-minute window as any other pending-payment order.
- Runs the scheduled job that finds orders past their pending-payment window and hands them back to the Order/Inventory services to cancel and release stock.

## Notification service

**What it does:** the only thing that turns internal events into actual messages — email, WhatsApp, or a dashboard entry. Keeps the Order/Inventory logic focused on business rules, not message formatting.

**Events it listens for:**

| Event | Who gets notified | How |
|---|---|---|
| `order_confirmed` | Staff | Dashboard alert inbox (email once a provider is wired up) |
| `order_confirmed` | Customer | **WhatsApp message.** Email only when one was collected, which in Phase 1 is never |
| `order_status_changed` (Packed/Shipped/Delivered) | Customer | WhatsApp push |
| `low_stock_breach` | Staff | Dashboard alert inbox |
| `order_modified` / `order_cancelled` (automatic) | Staff | Dashboard alert inbox (visibility, not approval) |
| `item_swap_requested` | Staff | Dashboard swap queue |
| `order_delivered` | Customer | WhatsApp feedback request |

**On the new customer confirmation:** the WhatsApp message goes out using the phone number collected at checkout — which is always present, since phone is a required field on every channel, including the website. That means a customer who orders on the website or TikTok still gets a WhatsApp confirmation, not just an email. This is deliberate: WhatsApp has far higher open rates than email, so it doubles as the most reliable way to make sure an order confirmation is actually seen, independent of where the order was placed.

## Webhook handling

Every message from WhatsApp, Meta, or TikTok arrives as an HTTP request to the backend. Two things happen before it's trusted:
1. **Signature verification** — confirms the request genuinely came from that platform (a signature check against a shared secret), not just "any request that showed up."
2. **Idempotency check** — platforms retry webhook delivery if they don't get a fast enough response, so the same message can arrive twice. The handler checks "have I already processed this exact message ID?" before acting, so a retried webhook can't create a duplicate order or double-charge stock.

## Interactions with other components

- **Website** → calls the Order/Inventory/Payment/Discount logic directly (a normal API call, no chatbot layer involved).
- **WhatsApp / FB DM / IG DM / TikTok DM** → messages arrive via webhook, get processed by the Chatbot Orchestrator (see `02-chatbot.md`), which then calls into these same services.
- **All five databases** → read/written exclusively by this backend.
- **Admin dashboard** → reads order/product/client/discount data, and writes staff actions (approving an item swap, editing a threshold, creating a discount code).
- **Email provider / WhatsApp API** → outbound only, via the Notification service.

## Edge cases worth knowing about

- **Payment confirmation arrives right as the timeout fires** — a race between the scheduled cancellation job and the payment gateway's webhook. Handled by checking the order's current status before acting: if it's already been cancelled, a late payment confirmation gets refunded rather than silently reviving a cancelled order.
- **Discount code hits its usage limit mid-checkout** — two customers using the last remaining use of a code at the same moment. The `times_used` increment is part of the same atomic transaction as the order, so only one of them succeeds; the other sees "this code is no longer available" before their order commits.
- **Stock insufficient at the final re-check** — customer is told immediately, before anything is written, rather than after.
- **One variant of a product sells out mid-conversation while its siblings don't** — the refusal names that variant and returns the siblings that are still in stock, so the customer can switch size in one message rather than being told the product is gone (see `02-chatbot.md`).
