# 09 — Order database

## Purpose

The record of every order, from the moment it's placed to however it ends — the backbone that tracking, modification, feedback, and analytics all hang off of.

## Fields

| Field | Type | Notes |
|---|---|---|
| `order_id` | Primary key | Format `WNS-<n>`, starting at 1001 |
| `client_id` | Foreign key | Present even for guest orders |
| `source_channel` | Enum | website / whatsapp / facebook_dm / instagram_dm / tiktok_dm |
| `items` | List | One entry per line: a `variant_id`, a quantity, and a snapshot — see below |
| `shipping_address` | Text | **Copied onto the order**, not read from the Client DB — see below |
| `contact_phone` | Text | Copied at order time; also what the WhatsApp confirmation is sent to |
| `governorate` | Text | Chosen from a fixed list — it's what sets the shipping fee, so it can't be free text buried in the address |
| `subtotal` | Number | Items only, after any per-item sale price |
| `discount_code_used` | Text, nullable | |
| `discount_amount` | Number | |
| `shipping_fee` | Number | Copied from the rate table at order time, not looked up later |
| `total` | Number | `subtotal - discount_amount + shipping_fee` — what the customer actually pays on delivery |
| `status` | Enum | See status list below |
| `payment_method` | Enum | Card / wallet / COD |
| `payment_status` | Enum | `pending` / `paid` / `failed`. A cash-on-delivery order stays `pending` until it is delivered and the courier settles |
| Status timestamps | One per stage | Drives the WhatsApp tracking pushes |
| `modification_log` | List | What changed, when, and via which channel |

## What an order item records

An item is not just "which product" — the catalog has three independent choice axes and the warehouse can't pack the order without all of them:

| Field | Notes |
|---|---|
| `variant_id` | What was actually bought — the reference back to the catalog |
| `product_name` | Copied at order time, so a later rename doesn't rewrite order history |
| `size` | Copied |
| `color` | Copied. Present on every item — colour is a choice on every product |
| `length` | Copied; null except on the Worker Jacket |
| `quantity` | |
| `unit_price` | Price paid per unit, frozen at order time |
| `unit_original_price` | The pre-sale price at order time — 14 of the 18 products have discounted variants, so without this there's no record of what the customer thinks they saved |

**Price is copied, never looked up later.** Reading `price` from the Product DB when displaying an old order would show today's price for a purchase made at last month's — which turns every price change into a silent rewrite of order history, and makes revenue figures drift.

**The four money columns are stored separately, not derived on read.** A single "total" that means different things in different views is how revenue reporting goes wrong: shipping isn't revenue, and a discount has to be visible as its own number to be reportable. Storing `subtotal`, `discount_amount`, `shipping_fee` and `total` means every later question — margin, discount depth, delivery cost — is answerable without recomputing anything against a rate table that has since changed.

**`shipping_fee` is copied, like every other price.** Rates change; a delivered order's total must not.

**The shipping address is copied for the same reason,** and for one more: the chatbot lets a customer ship a single order somewhere other than their saved address ("a one-time location", see `02-chatbot.md`). If the order only held a `client_id` and the address lived solely in the Client DB, that one-time address would have nowhere to live, and every past order would silently re-point at the customer's current address the moment they moved — including orders already delivered to the old one.

**The size/colour/length are copied even though `variant_id` already implies them.** The variant is the reference; the copies are the snapshot. A variant can be renamed, retired, or corrected in the catalog, and the packing slip for an order placed last month has to still say what was sold. Reading them back through the foreign key would make the catalog able to rewrite history.

## Status list (updated)

```
Pending payment → Confirmed → Packed → Shipped → Delivered
                 ↘ Cancelled (timeout or customer request)
```

- `Pending payment` only applies to card/wallet orders — cash on delivery skips straight to `Confirmed`.
- Once `Shipped`, no further modifications or cancellations are accepted (see `02-chatbot.md` for the modification rules).

## How it's written to

- **Order service** — creates the record as part of the atomic transaction, and updates status as an order moves through its lifecycle.
- **Payment service's timeout job** — moves an expired `Pending payment` order to `Cancelled`.
- **Chatbot Orchestrator / website** — appends to `modification_log` on a customer-driven change.
- **Admin dashboard** — staff manually update status (e.g. marking Packed → Shipped) and approve item-swap modifications.

## How it's read

- WhatsApp tracking pushes (status change detection)
- Customer-facing order history (website, and "which order?" lookups during modification requests)
- Admin dashboard order list/detail views
- Analytics — nearly every sales/fulfillment metric starts here

## Edge cases worth knowing about

- **Revenue metrics should exclude `Pending payment` and `Cancelled` orders** — otherwise unpaid or abandoned orders inflate reported revenue (see `13-analytics.md`).
- **A customer has multiple open orders** — the chatbot asks which one before applying any modification, rather than guessing based on recency.
- **Attribution for analytics when an order is modified on a different channel than it was created on** — `source_channel` always reflects where the order was *placed*; the channel used for a later modification is recorded separately in `modification_log`, not overwriting `source_channel`.
