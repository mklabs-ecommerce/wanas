# 16 — Supporting tables

The tables Phase 1 needs that aren't one of the four headline databases. Small, but every one of them is load-bearing — they were the gap that made the schema look complete when it wasn't.

## `channel_identities`

Maps a platform's ID to a customer. WhatsApp gives a phone number, Facebook and Instagram give a page-scoped user ID, TikTok gives its own.

| Field | Notes |
|---|---|
| `channel` | `whatsapp` / `facebook_dm` / `instagram_dm` / `tiktok_dm`. Part of the key |
| `external_id` | The platform's ID. Part of the key |
| `client_id` | Foreign key, **nullable** |
| `paused_until_staff_reply` | Boolean — set by `request_human`, cleared only from the dashboard |
| `first_seen_at` / `last_seen_at` | |

**`client_id` stays null until the customer confirms the link.** This is the single most misread thing in the schema: you can have a live conversation, a session, and a cart belonging to nobody, right up until the first order. Any code that assumes a conversation has a client will fall over on the first new customer. See `07-client-database.md` for why the link is confirmed rather than automatic.

**Cross-channel identity is never merged automatically.** The same person on WhatsApp today and Instagram next week is two rows, and stays two rows unless they confirm.

## `sessions`

Conversation history for the agent loop, keyed by `channel` + `external_id`.

| Field | Notes |
|---|---|
| `channel` + `external_id` | Composite key |
| `history` | JSON: the neutral message list from `02-chatbot.md` |
| `updated_at` | Drives expiry |

- **In the database, not in process memory.** The server has to be restartable without customers losing their conversation, and more than one instance has to be able to run behind a load balancer.
- **Cap: 40 messages.** Trimming **must start at a user message** — cutting between a tool call and its result leaves the history malformed and providers reject the entire request. This is the easiest thing in the whole system to get wrong and the hardest to diagnose.
- **Expiry: 6 hours** of silence, then the next message starts fresh.
- Losing a session loses nothing real. The cart is stored separately and survives.

## `carts` / `cart_items`

One open cart per channel identity. No separate `carts` table is needed at this scale — the identity is the cart key.

| Field | Notes |
|---|---|
| `id` | |
| `channel` + `external_id` | Whose cart |
| `variant_id` | Foreign key. **The only thing that can be in a cart** |
| `quantity` | 1–10 |
| `added_at` | |

- **Nothing here reserves stock.** An abandoned cart costs nothing, which is what makes it safe to keep them indefinitely.
- Cleared when `confirm_order` succeeds.

## `shipping_rates`

| Field | Notes |
|---|---|
| `governorate` | Primary key |
| `fee` | EGP |
| `updated_at` / `updated_by` | |

- **Seeded from `data/governorates.json`** — all 27, `fee: null` until the shop sets it. An order for a governorate with no fee is refused rather than shipped free.
- **Keys are English and stable, labels are Arabic.** The bot talks Arabic and must show Arabic names, but the stored key is ASCII — otherwise every spelling variant becomes a different row and the primary key stops being stable. Match customer input against both, and against common variants ("القاهره", "مصر الجديدة" → Cairo).

## `staff_queue`

The three review queues in `12-admin-dashboard.md` are one table with a `kind`, not three. They share every field that matters, and staff work them from one list.

| Field | Notes |
|---|---|
| `queue_id` | e.g. `SWAP-88` |
| `kind` | `item_swap` / `handoff` / `alert` |
| `status` | `open` / `resolved` / `rejected` |
| `channel` + `external_id` | The conversation it came from, when there is one |
| `order_id` | Nullable |
| `reason` | For a handoff: `unclear` / `complaint` / `customer_asked` / `image_received` / `out_of_scope`. For an alert: `order_confirmed` / `low_stock` / `order_modified` / `swap_requested` |
| `summary` | What the model or the event says it's about |
| `payload` | JSON — the requested swap, the attached photo, the alert's subject |
| `created_at` / `resolved_at` / `resolved_by` | |

- **Resolving a `handoff` row is what clears `paused_until_staff_reply`** on the channel identity. There is no other way out of a paused conversation.
- **Alerts live here too**, because in Phase 1 there is no email and this inbox is the only place staff see anything. One table means one unread count.
- Whichever staff action lands first wins; the second sees "already resolved" rather than double-applying.

## `staff`

| Field | Notes |
|---|---|
| `staff_id` | |
| `username` | |
| `password_hash` | Hashed, never reversible |
| `is_active` | Deactivate rather than delete, so the audit log keeps resolving |
| `created_at` | |

One role — everyone who can log in can do everything. See `12-admin-dashboard.md` for why, and for what that costs.

## `audit_log`

| Field | Notes |
|---|---|
| `id` | |
| `staff_id` | Who |
| `action` | e.g. `order.status_changed`, `variant.stock_edited`, `swap.approved` |
| `entity` / `entity_id` | What was touched |
| `before` / `after` | JSON, enough to see what actually changed |
| `at` | |

With a single role, attribution is the only control there is. "Who dropped that price to 5 EGP" has to be answerable.

## `webhook_events`

Idempotency for inbound platform webhooks.

| Field | Notes |
|---|---|
| `platform_message_id` | Primary key |
| `received_at` | |

Platforms retry delivery when they don't get a fast enough response, so the same message arrives more than once. Without this table a retry creates a duplicate order or double-decrements stock. Check before processing, insert as part of processing, and prune rows older than a few days.

## `size_charts`

Ships as `data/size_charts.json` and can stay a file in Phase 1 — 12 rows that change rarely. It becomes a table when staff need to edit measurements without a deploy.

| Field | Notes |
|---|---|
| `chart_id` | e.g. `wide-leg-sweatpants` |
| `title` / `image` / `unit` | |
| `measurements` | Rows with `label_en` and `label_ar` |
| `sizes` | Measurements per size |
| `length_specific` | True for `worker-jacket` only; absent elsewhere, and the tool fills in `false` |

## Not in Phase 1

- **`discounts`** — see `11-discount-database.md`.
- **Stock movement history.** Nothing records *why* a count changed, so `13-analytics.md`'s turnover and low-stock-frequency metrics have no source. That's acceptable while analytics is out of scope, but it's a schema decision that's expensive to backfill: if you want those numbers later, the cheapest time to start writing a `stock_movements` row on every decrement is now.
