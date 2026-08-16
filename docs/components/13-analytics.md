# 13 — Analytics

## Purpose

Turns the raw data sitting in the other databases into the numbers a manager actually checks day to day. No new data is created here — this is entirely a read layer over the five databases.

## Sales performance

| Metric | Source | Notes |
|---|---|---|
| Revenue over time | Order DB | `SUM(subtotal - discount_amount)` grouped by date, **excluding `Pending payment` and `Cancelled` orders** — otherwise unpaid or abandoned orders inflate the number. Not `SUM(total)`: that includes the shipping fee, which is a pass-through cost, not revenue |
| Delivery cost | Order DB | `SUM(shipping_fee)` by governorate — separate from revenue for the same reason |
| Average order value | Order DB | Same exclusion applies |
| Revenue by category/style/product/channel | Order DB + Product DB | Joined on order items. Report `category` and `style` separately — "how did hoodies do" and "how did oversized do" are different questions (see `08-product-database.md`) |
| Revenue by collection | Order DB + Product DB | Only meaningful for the two collections that exist; 8 products belong to none, so this can never be a full breakdown of revenue |
| Revenue by size / colour / length | Order DB | Straight off the order item's `size`, `color` and `length`. This is the metric that tells the shop what to restock, and it's why those fields are recorded per item rather than inferred |
| Discount depth | Order DB | `unit_original_price` vs `unit_price` on each item. Distinct from discount codes: 14 of the 18 products carry a standing sale price, so most of the margin given away never touches the Discount DB |
| Discount code impact | Order DB + Discount DB | Redemption rate = `times_used / usage_limit`; revenue split compares discounted vs full-price orders |

## Product & inventory

| Metric | Source |
|---|---|
| Best-sellers / slow-movers | Order DB, item counts over a rolling window |
| Stock turnover rate | Needs a `stock_movements` table that Phase 1 doesn't build — see `16-supporting-tables.md`. Backfilling it is impossible, so decide before launch whether to start writing it |
| Low-stock/sold-out frequency | Same source, same caveat — `status` is computed, not stored, so transitions leave no trace unless recorded |

## Customer insights

| Metric | Source |
|---|---|
| New vs returning | Order DB, grouped by `client_id` — the Client DB stores no order count or first-order date, so both are derived |
| Repeat purchase rate | Order DB, grouped by `client_id` |
| Customer lifetime value | Order DB, summed per client |
| Guest vs account share | Client DB `has_account` |
| Geographic distribution | Order DB shipping address |

## Channel performance

| Metric | Source |
|---|---|
| Orders per channel | Order DB `source_channel` |
| DM-to-order conversion | Chatbot session logs vs completed orders per channel |
| Comment auto-reply engagement | Facebook/Instagram comment logs |

## Fulfillment & operations

| Metric | Source |
|---|---|
| Average time per status stage | Order DB status timestamps |
| Cancellation rate & reasons | Order DB status + modification_log |
| Modification rate, item-swaps awaiting review | Order DB modification_log + admin queue |

## Customer satisfaction

| Metric | Source |
|---|---|
| Average rating, distribution | Order Feedback |
| Sentiment trends | Order Feedback free text — a lightweight keyword/sentiment pass, not full NLP, is enough at this scale |
| Complaint-escalation volume | Comment auto-reply flags + chatbot human-handoff queue |

## Computation approach

At 500+ orders/month, most of these can be computed live, on demand, without a separate batch pipeline — the query volume isn't high enough to need pre-aggregation yet. The one exception worth flagging: **customer lifetime value and repeat-rate metrics get more expensive to compute live as order history grows**, since they scan a client's entire order history rather than a recent window. Worth revisiting as a periodic (e.g. nightly) rollup once that becomes noticeably slow — not a v1 concern.

## Interactions with other components

- **Reads from:** Order DB, Order Feedback, Product DB, Client DB, Discount DB.
- **Writes:** none — this is purely a reporting layer.
- **Admin dashboard:** the display surface for all of the above.

## Edge cases worth knowing about

- **Channel attribution for a modified order** — always attributed to `source_channel` (where it was placed), not wherever a later modification happened, so channel performance reflects acquisition, not servicing.
- **A customer with only cancelled orders** — correctly counts as zero revenue and zero completed orders in sales metrics, but still shows up in raw order-count metrics if that's ever needed for a different question (e.g. "how many orders get cancelled").
