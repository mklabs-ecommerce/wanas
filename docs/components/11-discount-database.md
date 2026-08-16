# 11 — Discount database

## Purpose

Configurable codes, each independently scoped — no system-wide discount policy to maintain, just individual codes with their own rules.

## Fields

| Field | Type | Notes |
|---|---|---|
| `discount_id` | Primary key | |
| `code` | Text | What the customer types |
| `type` | Enum | Percentage or fixed amount |
| `value` | Number | e.g. `20` for 20%, or `20` for  20 EGP off |
| `applicable_products` | List, nullable | `product_id` values; empty = applies to everything. Scoping by `category` or `collection` instead is the more useful shape in practice ("20% off WINTER COLLECTION") — worth deciding when this comes into scope |
| `start_date` / `end_date` | Date, nullable | Empty = active until manually disabled |
| `usage_limit` | Integer, nullable | Total uses allowed |
| `per_client_limit` | Integer, nullable | e.g. once per customer |
| `times_used` | Integer | Counter |
| `status` | Enum | Active / inactive |

## How it's written to

- **Admin dashboard** — staff create and edit codes.
- **Order service** — increments `times_used` as part of the atomic order-creation transaction, so a code can never be over-redeemed even under concurrent use (see `01-backend-platform.md`).

## Validation logic (recap, with the important timing detail)

Checked at two points, not just one:
1. **When the customer enters the code** — active, in date, usage not exhausted, matches at least one cart item.
2. **Again at final order confirmation** — since time passes between entering a code and confirming an order, and someone else may have used the last redemption in the meantime.

**`times_used` only increments on a confirmed order** — not on the initial validation check. This matters: if it incremented at validation time, an abandoned cart (customer enters a code, then never finishes checkout) would incorrectly consume one of the code's limited uses.

## How it's read

- Website checkout (discount field validation)
- Order service (final validation + total calculation)
- Analytics (redemption rate, revenue from discounted vs full-price orders)

## Edge cases worth knowing about

- **A code lands on an already-discounted product** — 14 of the 18 products carry a standing sale price (`on_sale`, see `08-product-database.md`), so this is the normal case, not the exception. Whether a code stacks on top of a sale price or is refused on sale items is a margin decision the shop has to make before this ships; either way it needs to be an explicit rule, since "discount off a discount" on most of the catalog adds up fast.
- **A code is scoped to specific products, and the cart has both matching and non-matching items** — the discount applies only to the matching items; the customer sees which items it applied to so the total isn't confusing.
- **Per-client limit vs total usage limit both set** — both are checked; whichever is hit first blocks further use of that code.
