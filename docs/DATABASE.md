# Database

PostgreSQL in production, SQLite for local development only. 22 tables, no
ORM migrations tool — schema is reconciled at startup. This document covers
the structure, the relationships, the lifecycle of each kind of row, and the
consistency rules that are not visible from the schema alone.

---

## Engine and connection

`domain/db.py` owns the engine and `session_scope()`, the transaction helper
**everything** writes through.

```
DATABASE_URL=postgresql+psycopg://user:password@host:5432/dbname
```

- **Scheme rewriting.** `postgres://` and `postgresql://` are both rewritten
  onto `postgresql+psycopg`. Hosts hand out the bare forms; SQLAlchemy maps
  both onto the psycopg2 dialect, which is deliberately not installed — without
  the rewrite either spelling crash-loops the deploy on `ImportError` before a
  single request. Only the scheme changes; credentials are never moved,
  logged or rewritten.
- **SQLite in a deployment is refused.** A deployed container's filesystem is
  ephemeral: every session, client, order and queue row would be wiped on the
  next redeploy while the startup seed refilled the catalog — so the shop
  looks alive with nobody's history in it. Detected by the presence of any
  deploy marker env var. `ALLOW_SQLITE_IN_DEPLOY=1` overrides it for someone
  who genuinely means it.
- **SQLite parity pragmas** (local only): `foreign_keys=ON` (off by default in
  SQLite, always on in Postgres — a constraint bug must not wait for
  production to appear) and `journal_mode=WAL`. Every transaction opens
  `BEGIN IMMEDIATE`, so two concurrent order transactions queue rather than
  one dying with "database is locked" halfway through.
- `pool_pre_ping=True`, `expire_on_commit=False`, `autoflush=False`.
- After the session closes — and with it the write lock returns to the pool —
  `run_after_close` runs anything an after-commit hook could not do itself.
  **An outbound message must never describe a write that later rolled back**,
  which is what `common/events.py` exists for.

---

## Schema management

**No Alembic.** Tables are created at startup by `Base.metadata.create_all`.
That adds missing *tables* and silently ignores missing *columns* — a database
predating a model change boots looking perfectly healthy and then fails at the
first write mentioning the new column. That is not a degraded feature; it is
every write to that table failing.

So startup also reconciles columns:

- `domain/schema_drift.py` compares the models against the live schema;
- `_ensure_schema_columns()` in `app.py` adds what is missing — **additively
  and idempotently**, only `ALTER TABLE … ADD COLUMN`, never a drop, a retype
  or a backfill;
- a `NOT NULL` column with no server default is **reported, never guessed at**;
- `AUTO_MIGRATE_SCHEMA=0` reports the drift and changes nothing;
- `python scripts/migrate_schema.py --apply` does it by hand.

A schema check must never be what stops the app booting: failures are logged
and swallowed.

Seed a new database with `python manage.py seed`. Startup also auto-seeds if
the catalog tables are empty, because an unseeded production database is a
silent total failure — the process is healthy, `/health` says everything is
configured, and every customer search comes back "we don't have that".

---

## Tables

### Catalogue

| Table | Key | Notes |
|---|---|---|
| `products` | `product_id` (str) | local metadata the commerce platform has no field for: `category`, `department`, `style[]`, `collection`, `size_chart`, `size_chart_image`, `images[]`, `color_images{}`, `description`, `archived`. `price`/`original_price`/`on_sale` are **seeded values**, overlaid by live reads. |
| `variants` | `variant_id` (str) | FK → products. `size`, `color`, `length`, `price`, `stock_qty`, `low_stock_threshold`. |
| `size_charts` | `chart_id` (str) | `measurements[]`, `sizes{}`, `unit`, `image_url`. A dashboard-made row overlays the shipped JSON file on a shared `chart_id`. |
| `shipping_rates` | `governorate` | `label_ar`, `fee` (nullable — NULL means "no rate set", which refuses an order rather than guessing). |

> **`variants.stock_qty` is not the source of truth.** Anything deciding
> whether a sale may happen reads the live overlay
> (`catalog.live_stock(variant)`), never the column. Reading the column
> refused sizes the platform was selling — and because that refusal joins the
> stock waitlist, it later told those customers the item was "back in stock"
> when it had never left.

### Customers and conversations

| Table | Key | Notes |
|---|---|---|
| `clients` | `client_id` (int) | `full_name`, `phone` (idx), `email` (idx), `address`, `governorate`, `status`. |
| `channel_identities` | (`channel`, `external_id`) | `client_id` **nullable** — a conversation, session and cart can all belong to nobody until the first order. `paused_until_staff_reply`, `pending_link` (an unconfirmed match), `username`/`profile_name` (the platform handle), `last_seen_at` (the 24-hour window clock). |
| `sessions` | (`channel`, `external_id`) | `history` (JSON list of messages), `context_start` (int), `updated_at`. |
| `cart_items` | `id` | `channel`, `external_id`, `variant_id`, `quantity`, `added_at`. |

### Orders

| Table | Key | Notes |
|---|---|---|
| `orders` | `order_id` (str, `PREFIX-<n>`) | FK → clients. `source_channel` + `source_external_id` (which conversation placed it — status pushes go back down the same thread). Money split into `subtotal`, `discount_amount`, `shipping_fee`, `total`. Timestamps per stage. `modification_log` (JSON). `shopify_order_id` (idx) / `shopify_order_name`. |
| `order_items` | `id` | FK → orders, FK → variants. `product_name`, `size`, `color`, `length` and `unit_price` are **copied at sale time, never looked up later** — reading today's price for last month's purchase turns every price change into a silent rewrite of order history. |
| `order_feedback` | `feedback_id` | UNIQUE on `order_id` — one rating per order. |

### Staff and operations

| Table | Key | Notes |
|---|---|---|
| `staff` | `staff_id` | `username` UNIQUE, `password_hash` (PBKDF2-HMAC-SHA256, 240k rounds), `role`, `permissions[]`. **A NULL `role` reads as owner** — every account predating permissions. The opposite ("scoped to nothing") locks everyone out of the one screen that hands permissions out. |
| `staff_queue` | `queue_id` (str) | `kind` (handoff / item_swap / alert), `status`, `reason`, `summary`, `payload`, and optional channel/external_id/order_id. |
| `runtime_settings` | `key` | staff-toggleable feature flags. |
| `test_phone_numbers` | `phone` | numbers exempted from customer-facing behaviour. |
| `counters` | `name` | `order_id` / queue-id sequences. Deriving the next id from `max(order_id)` races under concurrent checkout; a row updated in place inside the same transaction does not, on either database. |

### Integration bookkeeping

| Table | Key | Lifecycle |
|---|---|---|
| `webhook_events` | `platform_message_id` | Written per inbound delivery as the idempotency claim. Deleted by `release_claims` on a failed turn, and **pruned past its retry window** by `domain/services/retention.py` on the scheduler (`WEBHOOK_EVENT_RETENTION_DAYS`, default 30). Without the prune this table is append-only for the life of the deployment. |
| `stock_waitlist` | `id` | `observed_stock` is the baseline that makes "back in stock" a *verified transition* rather than "positive today". `notified_at` marks it handled. |
| `abandoned_cart_nudges` | (`channel`, `external_id`) | `sent_at`; a new cart line moves `last_activity` past it and re-arms the nudge. |
| `whatsapp_media` | `path` | local file → uploaded media id, so a size chart is uploaded once. |
| `integration_tokens` | `provider` | the refreshed long-lived channel token and its expiry. |
| `instagram_comment_replies` | `comment_id` | written **before** any send, so a crash cannot permit a second public reply or DM. Also the record the comments screen reads: the comment text as it was, the exact public and private lines that went out, `category`, `sentiment`, `commenter_username`. |

---

## Relationships

```
clients 1─┬─* channel_identities        (client_id nullable until confirmed)
          └─* orders ─* order_items ─* variants ─* products
             └─1 order_feedback                        └─ size_charts (by id)

channel_identities (channel, external_id) ──┬── sessions        (same key)
                                            ├── cart_items
                                            ├── stock_waitlist
                                            ├── abandoned_cart_nudges
                                            └── staff_queue (optional)

staff ─* staff_queue.resolved_by
      ─* shipping_rates.updated_by
      ─* runtime_settings.updated_by
```

`order_items.variant_id` is a real foreign key, and that single fact is what
guards product deletion: anything that has ever been sold is refused deletion
and offered **archive** instead (platform `status: ARCHIVED` plus
`products.archived`), which takes it off the storefront and out of the bot's
search while the order lines still read.

Either way `release_variants` lets go of everything else waiting on those
SKUs — cart lines, waitlist entries, the dead replacement on any open swap.
**Archiving needs that more than deleting does**: the variant still exists, so
nothing errors and the customer simply waits forever.

---

## The session row in detail

The one table whose shape is not obvious.

```
history       [ {role, content, ...}, ... ]   append-only, JSON
context_start  int                            where the LIVE slice begins
```

- `session.load()` → `history[context_start:]`, bounded by `HISTORY_CAP`.
- `session.transcript()` → the whole thing, archive included. **The dashboard
  must use this**, because a read must never be what ends a conversation.
- A conversation *ending* — six hours idle, a staff reset, the cap — moves
  `context_start` forward and **deletes nothing**.
- `SESSION_ARCHIVE_CAP` (2000) bounds the whole row so it cannot grow without
  limit.
- `session.purge()` is the only thing that deletes, and nothing calls it.

### Message keys

`assistant/messages.py` owns the shape. Storage-only keys never reach a
provider — every translation layer rebuilds its request from
`role`/`content`/`tool_calls`:

| Key | Meaning |
|---|---|
| `at` | ISO-8601 UTC, stamped on every stored message |
| `receipt` | outbound only: `sent`/`delivered`/`read` + when |
| `mids` | the platform ids this message was sent or received as |
| `mid_labels` | per-photo: what it showed and which product |
| `provisional` | set by `record_inbound`, cleared when the turn folds it in |
| `by` | `system` for shop-initiated messages — a third voice beside the model and staff, and one the `unanswered` filter skips |
| `delivered` | False when the message was written but could not be sent |

Messages stored before `at`/`receipt` existed show no time and no tick rather
than a guessed one. Read receipts deliberately do **not** touch
`updated_at` — that column is the inbox sort key and the expiry clock, and a
customer reading a message is not a new one.

---

## Consistency rules

1. **`session_scope()` is the only way writes reach the database.** Stock
   decremented but no order written is an inventory count that is permanently
   wrong with nothing to show for it.
2. **The order transaction is the one to run on PostgreSQL before deploying.**
   SQLite and Postgres do not serialise a concurrent decrement the same way,
   and this has already cost production once: `func.max(a, b)` works in SQLite
   (overloaded as a 2-argument scalar) and is `UndefinedFunction` in Postgres,
   unconditionally — it passed every test on the SQLite default and broke
   *every single order placement* in production at the last step, after the
   remote order had already been created. `CASE WHEN` is what is used now.
   `WANAS_TEST_DATABASE_URL=postgresql+psycopg://… make test`.
3. **A turn is not the only writer to `sessions`.** `run_turn` holds history
   in memory for the whole tool loop while a tool can write to the same row;
   the end-of-turn save passes `merge_since` and folds those messages back in.
4. **Post-commit sends, never in-commit sends.** The committing connection
   holds the write lock until the hook returns.
5. **Nothing writes `stock_qty` through the order path except
   `record_sold`/`record_returned`.** The platform already decremented.
6. **Prices on `order_items` are copies.** Never re-derive them.
7. **`webhook_events` claims may only be pruned past the longest platform
   retry window.** Pruning earlier re-opens the duplicate-order door.

---

## Operational notes

- Every test gets a fresh schema (`tests/conftest.py`), and an ambient
  `DATABASE_URL` is **ignored** — the suite drops schemas, so only
  `WANAS_TEST_DATABASE_URL` may aim it somewhere.
- Backups are the hosting platform's Postgres backups; nothing in this
  application manages them.
- Growth is dominated by `sessions.history` (bounded per conversation by
  `SESSION_ARCHIVE_CAP`) and, previously, `webhook_events` (now bounded by
  retention). Everything else grows with real business volume.
