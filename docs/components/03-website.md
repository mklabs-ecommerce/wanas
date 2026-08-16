# 03 — Website

## Purpose

The only channel with full visual browsing — and the channel every other integration effectively mirrors in miniature. It talks to the backend directly, without going through the Chatbot Orchestrator, since there's no conversation to manage: it's a normal web app making API calls.

## Catalog browsing

- Products browsable by **category** (T-Shirts, Hoodies & Sweatshirts, …) and filterable by `style`, `department` and colour. **Collection is a browsing shelf, not navigation** — 8 of the 18 products belong to no collection, so it can't be the primary path to anything. See `08-product-database.md`.
- Stock status visible per item (in stock / low stock / sold out) — not just revealed at checkout.
- Sold-out variants are shown but disabled, so the customer sees the full run and that their combination specifically is the one that's out. Selecting a colour re-evaluates which sizes are still selectable, since availability is per combination, not per axis — the Cairokee T-shirt has XL in Black but not in Brown.
- Sale prices show both `price` and a struck-through `original_price` — this applies to most of the catalog, not a handful of items.
- Every product currently has a `size_chart`, shown on the product page; a product without one omits the link rather than showing an empty chart.
- "Low stock" is shown deliberately, not just "in stock" vs "sold out" — it's a mild urgency signal and it's honest, since it's the same status the Inventory service already computes.

## Cart

- Add/remove items, adjust quantity, see a running total.
- Client-side stock hints (e.g. graying out a sold-out size) are a convenience, not the source of truth — every cart action still gets validated against live stock server-side, since the client-side view can be stale by the time someone acts on it.

## Checkout

1. **Discount code field** — validated live against the Discount DB as described in the original design (active, dated, usage limits, product match). Applies only to matching items in the cart.
2. **Account or guest** — optional account creation; guest checkout always requires phone, email, and address regardless.
3. **Payment method** — card, wallet, or cash on delivery.
4. **Order review** — final summary before committing.
5. **Confirm** — calls the Order service directly, which runs the same atomic transaction and payment branch described in `01-backend-platform.md`.

## Accounts

- Optional. A registered account saves shipping details and shows order history plus past feedback given.
- Guest orders still create a Client DB record (`has_account = false`) so order history and feedback are preserved even without a login — see `07-client-database.md`. A phone/email match against an existing client prompts the customer to confirm the link rather than merging silently, so the checkout needs a confirmation step for it.

## Language & layout

- Arabic/English toggle, with the layout mirroring to right-to-left for Arabic rather than just translating text in a left-to-right frame.

## Interactions with other components

- **Order/Inventory/Payment/Discount services** — called directly via API, no chatbot layer.
- **Product DB** — read for catalog browsing and live stock.
- **Client DB** — read/written for accounts and guest checkout.
- **Order DB** — written on checkout, read for order history views.

## Edge cases worth knowing about

- **An item goes out of stock while it's sitting in someone's cart** — caught at the final server-side check during checkout, same as every other channel; the customer is told before payment, not after.
- **A discount code becomes invalid between "apply" and "place order"** (e.g. someone else just used the last redemption) — re-validated at checkout, not just when first entered.
- **Guest cart persistence** — held in browser storage until checkout; nothing about a guest's cart touches the database until they actually place the order.
