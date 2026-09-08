# Reusing this chatbot for another brand

This repository is one brand's deployment of a general system. The system is
the chatbot, its API, the commerce integration, and the staff dashboard; the
brand is a set of credentials, a catalogue, and about a dozen files of
customer-facing wording.

This document draws that line precisely: what you deploy unchanged, what you
must change, what you may want to change, and what you must not touch.

**Estimated effort for a like-for-like brand** (Shopify, WhatsApp +
Instagram, cash-on-delivery): a day for configuration and credentials, plus
however long the wording and the catalogue take.

---

## 1. Reusable as-is — the system

None of this contains brand-specific logic. Deploy it unchanged.

| Layer | What it gives you |
|---|---|
| `app.py` | composition root, startup reconciliation, `/health` |
| `assistant/runtime.py` | the one entry point every channel calls |
| `assistant/dispatcher.py` | debounce, worker threads, graceful flush |
| `assistant/agent.py` | the tool loop and every reply guard |
| `assistant/context.py`, `session.py`, `quoting.py`, `display.py` | memory, transcript, quoted replies |
| `assistant/providers/` | the LLM boundary — OpenRouter, Gemini, a scripted fake |
| `assistant/tools/base.py` | the tool registry, validation, implicit product resolution |
| `assistant/channels/` | the WhatsApp and Instagram adapters |
| `assistant/media.py` | voice notes and photos |
| `domain/models.py`, `db.py`, `schema_drift.py` | the schema and its reconciliation |
| `domain/services/` | orders, inventory, carts, notifications, queues, scheduler, retention, auth, staff admin |
| `integrations/` | Shopify, WhatsApp, Instagram, mail — all four vendors |
| `dashboard/` | the whole staff surface |
| `api/public_media.py` | the HMAC-gated media route |
| `common/` | money, events, signatures, path guards, identifiers, bidi |
| `tests/` | 1694 tests. Keep them; they encode the failures already paid for. |

**The tool *contracts* are reusable too.** The 19 tools describe a
conversational commerce shop generally: browse, size, price shipping, cart,
order, track, cancel, swap, escalate. Adding a tool is normal; changing what
an existing one refuses is not, unless your business genuinely differs.

---

## 2. Must change — credentials

Every one of these is per-brand, and none may ever be committed. See
[CONFIGURATION.md](CONFIGURATION.md).

### Commerce platform
`SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `SHOPIFY_WEBHOOK_SECRET`,
`SHOPIFY_VENDOR` (the brand name stamped on products this app creates).

### WhatsApp
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`,
`WHATSAPP_VERIFY_TOKEN`.

### Instagram
`INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_APP_SECRET`,
`INSTAGRAM_VERIFY_TOKEN`, `INSTAGRAM_USERNAME`, and — read this one —
`INSTAGRAM_APP_SCOPED_ID`.

> The account is handed out under **two different numbers**
> (`GET /me?fields=user_id` and `GET /me?fields=id`), and which of them
> appears as the sender on an echo is the platform's choice, not yours.
> Setting only one is how a bot ends up publicly replying to itself, which is
> the worst failure available on that channel. Fetch both and set both.

### Everything else
`LLM_PROVIDER` + the provider key, `DATABASE_URL`, `DASHBOARD_SESSION_SECRET`
(a fresh random 32+ bytes — **never** reuse another brand's), `ALERT_EMAIL_TO`
and a transport.

---

## 3. Must change — brand content

Twelve files. This is the real work.

### 3.1 The catalogue — `data/`

| File | What to replace |
|---|---|
| `products_seed.json` | your products. Only the fields the commerce platform has no home for: `category`, `department`, `style[]`, `collection`, images, per-colour images, description. **Price and stock are read live** — the values here are a fallback for when the platform is unreachable. |
| `size_charts.json` + `size-charts/` | your charts, or delete both if you do not sell sized goods |
| `governorates.json` | **your shipping regions.** Named for Egypt's governorates; the model is "a named region with a flat fee". Rename the file's contents, not the concept. |
| `images/` | your product photos |

The schema is documented in [`AGENTS.md`](../AGENTS.md).

### 3.2 The system prompt — `assistant/prompt.py`

The single most important file to rewrite. It carries:

- **who the bot is** — the brand name, what the shop sells, the surface it is on;
- **language and voice** — this deployment writes Egyptian Arabic with product
  names kept in Latin script, and follows the customer if they switch;
- **the refusal rules** — what it must never claim;
- **formatting** — plain text, lists, no Markdown.

Rewrite it whole. Do not translate it phrase by phrase: the voice is the
product, and a translated prompt reads like a translation.

Note the structure you must preserve: `SYSTEM_PROMPT` contains a **surface
line** that `build_system_prompt` swaps for a second channel, and it raises if
that line has drifted out — rather than silently telling an Instagram customer
they are on WhatsApp. Keep both the line and the guard.

### 3.3 Customer-facing text outside the prompt

These are constants, not model output, and every one is brand-voiced:

| Where | What |
|---|---|
| `domain/services/notifications.py` | order confirmation, `_STATUS_TEXT` per stage, `FEEDBACK_REQUEST_TEXT`, `BACK_IN_STOCK_TEXT`, `ABANDONED_CART_TEXT` |
| `assistant/runtime.py` | `IMAGE_ACK`, `VOICE_ACK` |
| `assistant/agent.py` | `RATE_LIMITED`, `GENERIC_FAILURE`, `LOOP_EXHAUSTED`, `TRUNCATED_FALLBACK`, `PROMISE_FALLBACK*` |
| `assistant/comment_faq.py` | the fixed public answers (shipping cost, delivery time, payment) |
| `assistant/comment_replies.py` | the interchangeable public lines, one bank per comment category |
| `assistant/recovery.py` | the resume instruction after a handoff is taken back |

Keep the **shapes**. `comment_replies` holds a *bank* per category rather than
one line, because two people asking the same thing used to get byte-identical
text back and a post filled with one repeated sentence. `PROMISE_FALLBACK` has
three variants because asking a customer to restate a product the bot named
two messages ago is what reads as amnesia.

### 3.4 Language matching — `domain/services/search_terms.py`

Maps how customers actually write product words onto the catalogue —
including transliterations. Entirely language- and catalogue-specific. If your
customers write one language in one script, this file gets much smaller.

### 3.5 Right-to-left layout — `common/bidi.py`

Only needed if your language is RTL. It wraps Latin runs in FSI/PDI at the
**send boundary only**, so the transcript and every search still see plain
text. For an LTR-only brand, make the wrap a no-op — do not delete the seam,
because the send boundary is the right place for it if you ever need it.

### 3.6 The privacy policy — `api/legal.py`

The page Meta requires a logged-out reviewer to be able to read. Your brand
name, your jurisdiction, your vendor table. **Have a lawyer read it.**

### 3.7 Dashboard translations — `dashboard/dashboard.html`

Bilingual, with the source language as the key. `tests/test_dashboard_i18n.py`
**fails** if a screen is added without its translations — keep that test, it
is what stops the dashboard drifting into one language.

### 3.8 Small identifiers

`app.py` FastAPI `title`; the OpenRouter `X-Title` header; the alert email
subject prefix in `alert_email.py`; the harness banner. Cosmetic, but they
show up in dashboards and inboxes.

---

## 4. Likely to need changing — business rules

Read [`AGENTS.md`](../AGENTS.md) first; it states the assumptions.

| Assumption in this deployment | If yours differs |
|---|---|
| **Cash on delivery only** | An order goes straight to `Confirmed` — no gateway, no pending-payment state, no timeout job, and no refunds anywhere. Adding online payment is a real project: a payment state machine, a webhook, a refund path, and a new tool. |
| **Flat fee per region** | `shipping_rates` is one fee per named region. Weight- or zone-based pricing means changing that table and `get_shipping_fee`. |
| **Sized garments** | `variants` carry size/colour/length. A brand with no sizes uses one variant per product; `get_size_chart` then answers "not applicable" rather than being deleted. |
| **`MAX_QUANTITY_PER_LINE = 10`** | a config value |
| **`SESSION_EXPIRY_HOURS = 6`** | how long before a conversation is treated as ended |
| **`ABANDONED_CART_HOURS = 2`** | how patient the nudge is |
| **Order id prefix** | `orders.next_order_id` and the queue prefixes |
| **Alert reasons** | `domain/services/alert_email.py` decides which queue items are worth an email. The reasoning transfers; the list may not. |

---

## 5. Do NOT change

Each of these encodes a failure that already happened. Changing one
reintroduces it.

### Security

- **`*_configured` vs `*_webhooks_configured` are different questions.** The
  first is "can we send", the second is "can we authenticate what arrives".
  Inbound must gate on the second. Merging them once left an unauthenticated
  public endpoint where any POST became a customer message — and a customer
  message can place a real cash-on-delivery order.
- **Never write `if secret and not verify_signature(...)`.** It reads as a
  convenience for local development and is the check turning itself off.
- **The two Meta app secrets are different strings** even inside one app.
  Never merge them.
- **`data/inbound` is never publicly servable** — those are customers' own
  photos and voice notes.
- **No secret means refuse**, everywhere: the three webhooks, the dashboard
  login, the public media route.
- **A NULL staff `role` means owner.** The opposite locks everyone out of the
  screen that hands permissions out.
- **The harness ships off.** It is unauthenticated by design and anyone who
  reaches it can converse as any customer identity.

### Data

- **Never replace PostgreSQL with SQLite in a deployment.** The filesystem is
  ephemeral; every order and conversation is wiped on redeploy while the seed
  refills the catalogue, so the shop looks alive with nobody's history in it.
- **Never remove chat/session persistence**, and never make it in-memory.
- **The transcript is append-only.** A conversation ending moves
  `context_start`; it deletes nothing.
- **Never summarise conversation history.** Compaction removes whole messages
  only, so every sentence the model reads is the exact sentence that was said.
- **Order status only moves forward, one stage at a time.**
- **Prices on order lines are copies.** Never re-derive them.
- **Only prune `webhook_events` past the longest platform retry window.**

### Order and inventory

- **Create the remote order before the local row.** A remote order with no
  local row is fixable; a local row the platform never heard of is stock sold
  twice.
- **Never decrement inventory manually in the order path.** The platform
  already did it on `orderCreate`; doing it twice silently oversells.
- **On a local failure, cancel the remote order** and say in the log whether
  the cancel succeeded.
- **Roll back to the savepoint, not the transaction** — that is what keeps the
  customer's cart.
- **Never read `variants.stock_qty` to decide a sale.** It is a seeded value
  nothing keeps current.
- **"Back in stock" must describe a verified transition**, not a positive
  number today.
- **Refuse an order when the platform is unreachable.** Do not degrade.

### Conversation

- **The webhook must never run the turn.** It claims, records and returns 200.
- **Record the inbound message on arrival**, in its own committed transaction,
  before the debounce window opens.
- **Record every shop-initiated message in the transcript too**, through the
  registered port — `domain/` must never import `assistant/`.
- **A stored message is not proof it arrived.** Decide deliverability inside
  the transaction that writes it.
- **A quoted reply is resolved, never guessed.** An unresolvable id is left
  unannotated.
- **A failed lookup never becomes the conversation's product.**
- **`get_sender()` with no channel is a bug** — it silently means the default
  channel.

### Public surfaces

- **Never answer your own account.** Check a *set* of self ids.
- **One private reply per comment, ever** — the row is written before the send
  precisely so a crash cannot permit a second.
- **The public comment surface never displays a sentence a model chose.** The
  model picks a category; the words come from a lookup.

### Deployment

- **The start command is `uvicorn app:app`.**
- **Dependencies and the Python version stay pinned.**
- **`CHATBOT_DEBUG` and `HARNESS_ENABLED` stay off.**

---

## 6. Swapping a platform, not just a brand

### A different LLM

Cheapest change here. Add a class in `assistant/providers/` implementing
`base.LLMProvider` and set `LLM_PROVIDER`. Nothing above that package may
import a vendor SDK — every provider is called over raw HTTPS.

Implement `supports_audio` / `supports_vision` **honestly**: they let the
runtime fall back to a person *before* spending a call finding out. And return
`""` from `transcribe()` on a truncated result — half a voice note does not
read as broken, it reads as a shorter message, and the whole turn is built on
it.

### A different commerce platform

The largest change. `integrations/shopify/` is ~6000 lines and the boundary is
not abstracted behind a `CommercePlatform` interface — this deployment has one
platform and paid for the concreteness. What transfers is the **contract**:

- live price and stock per message, matched by a stable key (SKU here);
- create the order remotely first, and cancel it if the local write fails;
- never decrement inventory yourself;
- push status changes back in via a signed webhook;
- degrade to local numbers for browsing, refuse for ordering.

Expect to rewrite the package and keep everything above it.

### A different channel

`assistant/channels/instagram.py` is the worked example of adding a second
one. An adapter must: verify its own signature, claim message ids under a
prefixed key, download media, call `record_inbound`, hand to its **own**
dispatcher, return 200 fast, and register an outbound sender **under its own
channel key**. It must never contain conversational policy — that lives in
`runtime.py` so every channel shares it byte-for-byte.

### A different host

Two host-specific things: SMTP is blocked (hence three mail transports), and
`RAILWAY_PUBLIC_DOMAIN` is the fallback for `PUBLIC_BASE_URL`. Set
`PUBLIC_BASE_URL` explicitly elsewhere. `railway.toml` is ignored by other
hosts; port the healthcheck to their equivalent.

---

## 7. A checklist for a new brand

```
Credentials
  [ ] commerce platform: domain, admin token, webhook secret
  [ ] WhatsApp: phone id, token, app secret, verify token
  [ ] Instagram: account id, token, app secret, verify token,
      username, AND app-scoped id
  [ ] LLM provider + key; a media model that can actually hear
  [ ] fresh DASHBOARD_SESSION_SECRET (never reuse another brand's)
  [ ] PostgreSQL DATABASE_URL
  [ ] ALERT_EMAIL_TO + a transport that works from your host

Content
  [ ] data/products_seed.json
  [ ] data/governorates.json  (your regions and fees)
  [ ] data/size_charts.json + size-charts/  (or removed)
  [ ] data/images/
  [ ] assistant/prompt.py                    ← rewrite, do not translate
  [ ] the customer-facing constants in §3.3
  [ ] domain/services/search_terms.py
  [ ] api/legal.py                           ← have a lawyer read it
  [ ] dashboard/dashboard.html translations
  [ ] app title, X-Title, alert subject prefix

Platform setup
  [ ] webhook subscriptions on all three vendors
  [ ] proactive message templates submitted and approved
      (without them nothing can be sent outside the 24-hour window)
  [ ] verify tokens matched on both sides
  [ ] a first staff account: python manage.py create-staff

Verify before opening the doors
  [ ] make check green
  [ ] the suite green against PostgreSQL
  [ ] /health: every integration true, catalog counts non-zero
  [ ] unsigned POSTs to all three webhooks refused (403/403/401)
  [ ] /harness 404, /dashboard/api/* 401
  [ ] a real message on each channel, end to end
  [ ] a real order placed, then fulfilled, and the status push arrives
  [ ] a real voice note and a real photo from a phone
  [ ] an owner alert actually lands in the inbox
```

The last five need a person. Nothing in the test suite can prove them.

---

## 8. What would make the next brand easier

Honest technical debt, not a plan:

- **The commerce platform is not behind an interface.** Concrete and correct
  for one platform; a second one means extracting `CommercePlatform` first.
- **Customer-facing strings are scattered across six modules** rather than
  living in one catalogue. A brand rewrite means finding all of them; §3.3 is
  that list, which is a workaround for not having the catalogue.
- **The default currency, region vocabulary and phone-number normalisation
  are Egypt-shaped.** `common/identifiers.py` and
  `identities.phone_variants` encode local number formats.
- **The dashboard is bilingual with one hard-coded pair.** The `TR` mechanism
  generalises; the bundle does not.

None of these blocks reuse. All of them are worth knowing before you promise a
timeline.
