# Data flow

The end-to-end paths, including the error and alternative branches — which are
usually where the interesting decisions are.

Companion to [ARCHITECTURE.md](ARCHITECTURE.md) (the layering) and
[CHATBOT.md](CHATBOT.md) (the agent internals).

---

## Flow 1 — A customer message becomes a reply

```
 1  Customer sends a message on WhatsApp / Instagram
 2  POST /webhooks/{channel}
 3      ├─ channel not configured for inbound? ────────────────► 503, stop
 4      ├─ signature invalid or absent? ────────────────────────► 403, stop
 5      ├─ sender is our own account (echo)? ───────────────────► drop, stop
 6      ├─ claim_message(platform id) already taken? ───────────► 200, drop
 7      ├─ download media to data/inbound/
 8      ├─ record_inbound()  ← own committed transaction
 9      └─ dispatcher.submit()                              ──► 200 returned
10
11  ── worker thread, after ~6s debounce, one turn per conversation ──
12
13  runtime.handle_message()
14      ├─ duplicate delivery? ─────────────────────────────────► stop
15      ├─ conversation paused for staff?
16      │     ├─ handoff resumable + customer wrote back? → take it back
17      │     └─ otherwise: store the message, no model call ──► stop (silent)
18      ├─ voice notes → transcribe each
19      │     └─ none transcribable → handoff + VOICE_ACK ─────► stop
20      ├─ photos → read each
21      │     └─ none readable → handoff + IMAGE_ACK ──────────► stop
22      ├─ nothing answerable left? ────────────────────────────► stop, warn
23      └─ open ONE Shopify snapshot for the whole turn
24
25  agent.run_turn()
26      ├─ build context: recall (compacted) + verbatim window
27      ├─ resolve quoted messages against the whole transcript
28      ├─ call the model with the prompt + context + 19 tool schemas
29      │     ├─ rate limit / 5xx → retry once after 30s
30      │     ├─ auth error → no retry, GENERIC_FAILURE
31      │     └─ finish_reason=length → regenerate shorter ×2 → fallback
32      ├─ tool calls? → validate args → run all → append results → loop
33      │                                   (capped at TOOL_LOOP_CAP = 8)
34      ├─ loop exhausted → LOOP_EXHAUSTED
35      └─ no tool calls → this text is the reply
36
37  guards: truncation · dangling promise · path leak · tool-call leak · markdown
38  save history (merge_since folds in anything a tool wrote mid-turn)
39
40  adapter sends: text (bidi-wrapped) → images → interactive
41  attach_outbound_ids(): stamp the platform ids onto the stored message
42  later: statuses[] callback → session.record_receipt (sent/delivered/read)
```

**Why the message is recorded at step 8, before any answer.** The dashboard
reads `sessions`. Until this existed, a row was only written *after* the model
answered — so a conversation still being thought about, one paused for a staff
member who could not see it, one whose turn crashed and rolled back, and one
the model simply never replied to all looked identical to a customer who never
wrote. The provisional copy is folded into the real message the turn stores;
one left behind is not litter, it is a message nobody answered, and the
inbox's `unanswered` filter is built on exactly that.

**Why the claim at step 6 is released on a crash.** The claim stops a retry
being processed twice. A copy that crashed mid-turn used to keep its claim
forever, so every retry of it was suppressed by design — silence with no way
back in. `release_claims` deletes the row; the success path never calls it.

---

## Flow 2 — Placing an order

The compensating transaction. Read this one carefully before changing it.

```
 1  confirm_order(name, governorate, address, phone[, email])
 2      ├─ any required field blank? ──────────────────► missing_fields
 3      ├─ cart empty?
 4      │     ├─ an order was placed here moments ago ─► already_confirmed
 5      │     └─ otherwise ────────────────────────────► cart_empty
 6      ├─ governorate unknown ────────────────────────► no_rate_set + valid list
 7      ├─ no shipping fee set for it ─────────────────► no_rate_set
 8      └─ client blocked ─────────────────────────────► client_blocked
 9
10  ── live stock read, OUTSIDE the per-turn snapshot ──
11      └─ platform unreachable ───────────────────────► store_unavailable
12
13  read-only pass per line
14      ├─ variant not on the platform ────────────────► store_unavailable
15      └─ short ──────────────────────────────────────► items_out_of_stock
16                                                        (+ what IS available)
17
18  ── orderCreate on the platform: creates the sale AND takes the stock ──
19      ├─ rejected, out of stock ─────────────────────► items_out_of_stock
20      ├─ rejected, other ────────────────────────────► store_unavailable
21      └─ unreachable ────────────────────────────────► store_unavailable
22
23  ══ from here the sale EXISTS remotely and its stock is gone ══
24
25  SAVEPOINT
26      ├─ record_sold  (local bookkeeping ONLY — never decrement again)
27      ├─ upsert the client
28      ├─ INSERT order + order_items (prices copied, never looked up later)
29      ├─ recompute totals
30      ├─ clear the cart
31      ├─ RELEASE SAVEPOINT
32      ├─ post-order notifications, in their OWN savepoint
33      │     └─ failure here rolls back only itself and is logged loudly:
34      │        the order stands, nobody has been told about it
35      └─ COMMIT  ← the durable point
36
37  on ANY exception between 25 and 35:
38      ├─ ROLLBACK TO SAVEPOINT (keeps the cart — it was filled before it)
39      ├─ log the stage that failed
40      ├─ cancel the remote order (puts the sale and the stock back)
41      └─ return order_failed{stage, shopify_order, shopify_cancelled}
42
43  after commit: confirmation message → customer + transcript + staff alert
```

Four decisions worth keeping:

- **Remote first, local second.** A remote order with no local row is visible
  in the admin and fixable; a local row the platform never heard of is stock
  that quietly sells twice, and the customer only finds out when nothing
  arrives.
- **Roll back to the savepoint, not the transaction.** The cart was filled
  *before* the savepoint opened, so this leaves it exactly as the customer
  built it. A full rollback threw the cart away too, and their next "yes,
  confirm" answered `cart_empty`.
- **Notifications cannot fail the order.** They are bookkeeping about an order
  that already exists. Letting them raise turned a completed order into
  `tool_failed`: the customer was told there was a technical problem while
  their order sat in the admin.
- **Inventory is decremented once, by the platform.** `record_sold` touches
  only the local row and cannot refuse. Calling `decrement` here would take
  every item twice.

---

## Flow 3 — Order status reaches the customer

```
Staff fulfil / cancel in the platform's admin
        │
        ▼
POST /webhooks/shopify
    ├─ no signing secret configured ──────────────────► 503
    ├─ bad base64 HMAC ───────────────────────────────► 401
    ├─ signed, but a different shop domain ───────────► 403
    ├─ delivery id already claimed ───────────────────► 200, drop
    └─ background task, 200 returned
            │
            ▼
    orders.advance_status()   forward only, one stage at a time
            │
            ▼
    notifications.record_status_push()   ← INSIDE the transaction
            ├─ window_open? → free-form text, delivered=True
            └─ closed?
                  ├─ approved template configured → send the template
                  └─ none → store delivered=False + raise a staff alert
            │
            ▼  (after commit, never inside it)
    deliver_status_push()  → the channel's sender
```

**Why the record is written inside the transaction and the send is not.** The
committing connection holds the write lock until the after-commit hook
returns, which is why `record_status_push` is separate from
`order_status_changed`. And **a stored message is not proof it arrived** — the
deliverability decision is made where the message is decided, not where it is
sent.

`SHOPIFY_WEBHOOK_SECRET` unset means **no status push ever fires**, and the
failure is silent: orders simply stay `Confirmed` forever. It is warned at
boot for exactly that reason.

---

## Flow 4 — An Instagram comment

```
comments webhook item
    ├─ commenter is our own account ─────────────────► drop
    ├─ comments disabled by flag ────────────────────► drop
    ├─ parent is our own reply thread ───────────────► drop (no self-loop)
    ├─ older than INSTAGRAM_COMMENT_MAX_AGE_HOURS ───► drop
    ├─ comment says nothing (emoji only, etc.) ──────► drop
    ├─ over the rate limit for this commenter
    │      ├─ it was an FAQ (no model call spent) ───► drop quietly
    │      └─ otherwise ─────────────────────────────► drop + comment_flood alert
    ├─ an InstagramCommentReply row already exists ──► drop (handled once)
    │
    ├─ WRITE the reply row  ← before any send, so a crash cannot
    │                          permit a second public reply or DM
    │
    ├─ matches a fixed FAQ?
    │      └─ public answer from comment_faq (a lookup) ──────────► done
    │
    └─ classify (the ONE model call on this path — category only)
           ├─ classifier unavailable ─────────────────► alert, say nothing
           ├─ staff alert for the category, if any
           ├─ DM budget exhausted? → downgrade to public-only, or nothing
           ├─ public line from comment_replies (a lookup, picked
           │    deterministically from the comment id)
           └─ DM handoff, or public reply, or neither
```

**The public surface never displays a sentence a model chose.** Every public
line is a lookup. The model picks a category; the words are ours.

---

## Flow 5 — Scheduled work

One clock (`domain/services/scheduler.py`), every
`REENGAGEMENT_INTERVAL_SECONDS` (default 1800). Each job swallows its own
failures so one cannot stop the others.

```
tick
 ├─ back-in-stock
 │    read the whole open waitlist, one platform read for all of it
 │    per entry, inside its own transaction:
 │      ├─ already notified ─────────────────► skip
 │      ├─ observed_stock is NULL ───────────► baseline it, say nothing
 │      ├─ observed_stock > 0 ───────────────► no restock happened, say nothing
 │      └─ was ≤0, is >0 now ────────────────► send + stamp notified_at
 ├─ abandoned carts
 │    MAX(added_at) per identity; due if idle between the min and max windows
 │      └─ already nudged for this idle spell ► skip
 ├─ channel token refresh (rate-limited to one attempt per day)
 ├─ catalogue import (mirror anything added in the platform's admin)
 └─ retention: prune webhook_events past their retry window
```

**"Back in stock" must describe a verified transition**, not a positive number
today. A local `stock_qty` gone stale at zero was once enough to announce a
restock for an item that had been on the shelf the whole time.

---

## Flow 6 — Staff replying from the dashboard

```
Browser → POST /dashboard/api/login  (JSON)
    ├─ no DASHBOARD_SESSION_SECRET ──────────► 503 (never sign an unforgeable-less cookie)
    ├─ wrong password / unknown user / deactivated
    │      → 401 invalid_credentials, and all three cost the same PBKDF2 time
    └─ ok → HMAC-signed cookie: staff_id.expires_at.signature
              httponly, samesite=lax, secure when the request was HTTPS

Every route → guard.require_permission(db, cookie, "<section>")
    ├─ no/expired/tampered cookie, or account deactivated ──► 401
    └─ no permission for this section ─────────────────────► 403
```

The sidebar hides sections an account cannot open. **That is a courtesy; the
route refusal is the control** — the endpoint behind a hidden button is one
`fetch` away.

A staff reply is recorded in the same history the model reads, so when the
conversation is handed back the bot knows what was already said.

---

## Flow 7 — A photo goes out, and a reply comes back to it

```
get_variants(product_id, color) → tool result carries image paths + labels
        │
        ▼
one send per image; each returns its own platform message id
        │
        ▼
session.photo_mid_labels():  id → { what it showed, which product }
        │
        ▼
customer replies to ONE of those photos (platform sends context.id)
        │
        ▼
quoting.referenced_product(): resolve the id to the PHOTO, not the message
        │
        ▼
last_product moves to that product; the turn answers about that colourway
```

Resolving to the *message* instead handed the model back all four colours the
customer had just pointed away from. An unlabelled photo — a product never
split by colour — falls back to quoting its message, which is still better
than naming a colourway nobody chose.

---

## Cross-cutting invariants

| Invariant | Enforced by |
|---|---|
| A message is processed at most once | `webhook_events` claim, taken at ingest |
| A crashed turn's message can be retried | `release_claims` on failure |
| The transcript shows arrival, not reply | `record_inbound`, own transaction |
| Every shop-initiated message is in the transcript | `register_transcript_recorder` |
| An outbound message never claims undelivered delivery | `window_open` inside the write |
| Stock is decremented exactly once | platform on `orderCreate`; local is bookkeeping |
| Order status only moves forward | `advance_status` |
| A conversation ends but is never deleted | `context_start` moves; only `purge()` deletes |
| No secret ⇒ refuse | all three webhooks, the dashboard login, public media |
