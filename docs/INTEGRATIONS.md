# External integrations

Every system this application talks to over the network, what it is trusted
for, how it is authenticated in both directions, and what happens when it is
unreachable.

Brand-agnostic: the vendors are the architecture, the accounts are
configuration. See [CONFIGURATION.md](CONFIGURATION.md) for the variables and
[REUSING_FOR_NEW_BRANDS.md](REUSING_FOR_NEW_BRANDS.md) for swapping the
accounts out.

---

## The map

```
                    ┌──────────────┐
                    │   Shopify    │  source of truth: price, stock, orders
                    └──────┬───────┘
              GraphQL Admin│▲ webhooks (base64 HMAC)
                    reads/ ││ orders/fulfilled · orders/cancelled
                    writes ││ fulfillments/update · products/create|update
                           ▼│
  ┌────────────┐    ┌──────────────────────────┐    ┌──────────────┐
  │  WhatsApp  │◄──►│                          │◄──►│  Instagram   │
  │ Cloud API  │    │      This application    │    │ Graph (Login)│
  └────────────┘    │                          │    └──────────────┘
   hex HMAC in      │                          │     hex HMAC in
   (own app secret) │        PostgreSQL        │     (own app secret)
                    └───────┬─────────┬────────┘
                            │         │
                    ┌───────▼──┐  ┌───▼────────────┐
                    │OpenRouter│  │ Resend / Gmail │
                    │  (LLM)   │  │  API / SMTP    │
                    └──────────┘  └────────────────┘
                     chat + media   owner alerts
```

Everything vendor-facing lives under `integrations/`, one package per vendor.
Nothing above `assistant/providers/` may import a vendor SDK — every provider
is called over raw HTTPS, so swapping one is a new class plus a config value.

---

## 1. Commerce platform — Shopify

**Trusted for:** live price, live inventory, and orders. Those three are read
from Shopify per message and written to Shopify on sale. Do not build a second
product database for them.

**Not trusted for:** catalogue metadata the platform has no field for — style,
department, collection, size charts, per-colour photos. Those live locally and
are overlaid with the live numbers (`domain/services/catalog.py`).

`variant_id` ↔ Shopify variant is matched by **SKU**, never by title: an admin
correcting a typo in a product name must not make the bot say the product does
not exist.

### Outbound (we call Shopify)

`integrations/shopify/client.py` — a minimal GraphQL Admin client over
`urllib` with a `certifi` SSL context, thread-safe, throttle-aware.

- Retries only what is genuinely transient: throttle, 5xx, network. A 401/403
  is a configuration mistake and retrying it only makes the customer wait
  longer for the same failure.
- `REQUEST_TIMEOUT` 8s for a customer-facing read — a reply that arrives after
  30s has already failed socially. Bulk/admin reads get a longer budget from
  `get_admin_client()`.
- Reads the `throttleStatus` extension and slows down deliberately below a
  floor.
- Every failure becomes `ShopifyUnavailable` or `ShopifyConfigError`. It never
  raises past its own boundary and never exits the process.

| Module | Does |
|---|---|
| `catalog.py` / `inventory.py` | live price, stock, variant reads |
| `orders.py` | `orderCreate`, cancel, the compensating path |
| `files.py` | the staged-upload dance (signed target → bytes → mutation) |
| `size_charts.py` / `size_chart_import.py` | charts ↔ product metafields, both directions |
| `product_import.py` | mirror a product created in the platform's own admin |
| `product_reconcile.py` | the only deleting direction — report at boot, delete by hand |
| `admin_*.py` | the dashboard's read/write surface |
| `webhook_registration.py` | subscribe the callbacks below |

### Inbound (Shopify calls us)

`POST /webhooks/shopify` — `integrations/shopify/webhooks.py`.

- **base64** HMAC-SHA256 of the raw body in `X-Shopify-Hmac-Sha256`. (Meta
  signs in hex; the two checks are deliberately separate implementations.)
- With no `SHOPIFY_WEBHOOK_SECRET` set, **every** delivery is refused with 503.
  That is correct behaviour, not a degradation.
- A correctly signed delivery for a *different* shop domain is refused 403 —
  applying it would move the wrong orders.
- The delivery id is claimed before the work is queued, so a retry cannot send
  a second "your order shipped".
- Work runs in a background task; the endpoint returns 200.

Topics: `orders/fulfilled`, `orders/cancelled`, `fulfillments/update` →
`orders.advance_status` → the customer's tracking message. `products/create`,
`products/update` → `product_import`.

### Degradation

| Situation | Behaviour |
|---|---|
| Unreachable while browsing | fall back to the local catalog's numbers, warn once |
| Unreachable while ordering | **refuse** the order — `store_unavailable` |
| Variant not on the platform | refuse to sell it; "sold out" would be a guess |
| Stock moved between read and write | `compareQuantity` makes the platform refuse; re-read and tell the truth |
| Webhook secret unset | no status pushes ever fire, and it is warned at boot |

---

## 2. Channel — WhatsApp (Meta Cloud API)

**Outbound** `integrations/whatsapp/client.py`: text (chunked), images
(`link` for http(s), uploaded media id for local files, cached in
`whatsapp_media`), interactive list/button payloads, approved templates, and
read receipts.

**Inbound** `POST /webhooks/whatsapp` — `assistant/channels/whatsapp.py`.

- `GET` serves Meta's `hub.challenge` handshake against `WHATSAPP_VERIFY_TOKEN`.
- `POST` requires **hex** HMAC-SHA256 of the raw body in
  `X-Hub-Signature-256`, keyed with `WHATSAPP_APP_SECRET`.
- Gated on `whatsapp_webhooks_configured` — token **and** app secret. This is
  deliberately stricter than `whatsapp_configured` (which only decides whether
  outbound can send): gating inbound on the weaker question once left an
  unauthenticated public endpoint, where any POST became a customer message
  and a customer message can place a real cash-on-delivery order.
- Always returns 200. Meta retries anything else; the idempotency claim is
  what makes a retry safe.
- Also carries `statuses[]` — delivery receipts for messages *we* sent
  (sent / delivered / read / failed), matched to a stored message by `mids`.

### Two kinds of customer identifier

Since April 2026 Meta also sends a **business-scoped user id** (BSUID) such as
`EG.1754797805572316`, and for a customer using a WhatsApp username it sends
*only* that — `from` and `wa_id` are omitted.

`common/identifiers.py` is the single place that tells the two apart. Two
rules follow, and both matter:

- outbound, a BSUID goes in `recipient`, **never** `to` — `normalise_recipient`
  strips every non-digit, so sending one through `to` addresses a different
  person;
- never run phone logic (variants, auto-linking a returning customer) over an
  identifier without checking `is_phone_number` first. Stripping `EG.` leaves
  digits that would link a stranger to someone else's address and order
  history.

### The 24-hour window

Meta refuses free-form business-initiated text more than 24 hours after the
customer's last message — routine for a status push on a parcel that ships the
next day. Every automated message decides deliverability **inside the
transaction that writes it**: window open → free-form; window closed → an
approved template, or nothing, recorded `delivered=False` with a staff alert.

Template names are configuration (`WHATSAPP_TEMPLATE_*`). Blank means that
message type simply cannot be delivered outside the window.

---

## 3. Channel — Instagram (Graph API, Instagram Login)

The same runtime, its own adapter, its own webhook, its own app secret — which
is a **different string** from the WhatsApp one even inside the same Meta app.

**Outbound** `integrations/instagram/client.py`: chunked text (no templates on
this channel), quick replies, images **by public HTTPS URL only** (Meta fetches
them itself), private replies to comments, public comment replies, and
`get_user_profile` to read a customer's @handle — read once per customer, not
per message, because it sits on the webhook path.

**Inbound** `POST /webhooks/instagram` — two surfaces flattened in arrival
order: `entry[].messaging[]` (DMs) and `entry[].changes[]` with
`field == "comments"`.

### The rules this channel cannot survive breaking

- **Never answer ourselves.** An echo, a `message_echoes` delivery, or
  anything whose sender id is in `settings.instagram_self_ids` is dropped
  before everything else. The check is a **set** because Instagram Login hands
  the same account out under two different numbers (`?fields=user_id` and
  `?fields=id`) and which one appears is Meta's choice.
- **One private reply per comment, ever.** The `InstagramCommentReply` row is
  written **before** the send, so a crash cannot permit a second one.
- **The public surface never displays a sentence a model chose.** Public
  replies come from `comment_faq.py` (one correct answer per question) or
  `comment_replies.py` (a bank of interchangeable lines per category, picked
  deterministically from the comment id so the same comment always gets the
  same reply and a post does not fill with one repeated sentence). Both are
  lookups. The model only picks a *category*.
- **Public images must be public.** Local files are served through the
  HMAC-gated `/public/media/...` route, and `data/inbound` — customers' own
  photos and voice notes — is unreachable through it even under a correctly
  computed token.

Comment handling is rate-limited per commenter per rolling hour, separately
for FAQ answers (which cost no model call) and for everything else, and
ignores comments older than `INSTAGRAM_COMMENT_MAX_AGE_HOURS`.

### The 60-day token

`integrations/instagram/token.py` refreshes the long-lived token, checked once
at boot and then on the scheduler (rate-limited internally to one attempt per
day). Without it the channel goes silent 60 days after launch with no other
symptom. `/health` reports `instagram_token_expires_at` so a broken refresh is
visible weeks early.

---

## 4. LLM — OpenRouter (default), Gemini, fake

`assistant/providers/` behind `base.py`. `LLM_PROVIDER` selects one.

**One key, one endpoint, two models.** Chat, voice-note transcription and
photo reading all go through the same `chat/completions` call. What
`LLM_MEDIA_MODEL` adds is a *model id*, because the cheapest model good enough
to run a conversation is not necessarily one with an audio endpoint at all —
the two therefore have separate defaults. There is no Whisper call and no
OpenAI key anywhere in this codebase.

- voice → an `input_audio` content part (base64 + a short format string)
- photo → an `image_url` content part carrying a base64 data URI

### A model id is not a serving stack

OpenRouter hosts one model id on many upstream providers and load-balances
between them per request. By default a provider that does not implement
`temperature` is **sent it anyway and silently drops it** — so a reply meant
to be sampled at 0.3 is generated at whatever that stack defaults to. The
first thing to degrade under that is non-English output, and it degrades *per
request*: most replies fine, the occasional one garbled.

`OPENROUTER_PROVIDERS` / `OPENROUTER_QUANTIZATIONS` pin the candidate set, and
the `require_parameters` filter they turn on is the part that matters — it
makes `temperature` a setting rather than a suggestion. Fallbacks stay on:
this excludes stacks that answer badly, it does not make one provider a single
point of failure. `unknown` quantization is excluded on purpose.

### Reasoning blocks

`reasoning_details` (OpenRouter) / `thoughtSignature` (Gemini) ride in
`ModelReply.signature` and are handed back with the assistant turn that
produced them. OpenRouter requires this for multi-turn tool calling: without
it the model gets its tool results back with no record of *why* it asked for
them, and answers the question it reconstructs rather than the one that was
asked. Each provider recognises only its own shape and drops the other.

### Never log a key in a URL

The Gemini key travels as a **header**, not `?key=...`, and `httpx`/`httpcore`
loggers are pinned to WARNING. A URL is not a safe place for a secret
precisely because so many things log one. Both locks are kept.

---

## 5. Email — Resend, Gmail API, SMTP

`integrations/mail/client.py` picks a transport and sends. Three of them,
because **the hosting platform blocks every outbound SMTP port** (25, 465,
587, 2525 all answer "Network is unreachable" from inside the container while
plain HTTP connects instantly). That is platform policy, not a setting, so an
app password cannot deliver from the deploy however correct it is.

Preference order:

1. **Resend** (HTTPS/443) — what production sends with, and first because it
   is the one transport with nothing in it that expires: one long-lived API
   key and one POST. `RESEND_FROM` must be a domain verified with Resend;
   blank falls back to their shared sender, which delivers only to the account
   owner — who is exactly who these alerts go to.
2. **Gmail API** (HTTPS/443) — same mailbox, no new vendor, no cost. The price
   is an OAuth refresh token that dies on a password change, a revoked grant,
   or after 7 days if the consent screen was left in Testing.
3. **SMTP** — works from a developer's machine and on a host that permits it.

*What* is worth an email is decided in `domain/services/alert_email.py`: a
complaint, a crashed or undeliverable turn, an order modified/cancelled/
swapped, low stock, and **every** handoff — because a handoff pauses the
conversation and nobody is answering that customer until a person looks. Order
confirmations are deliberately left out: they fire on every sale, and an
address carrying those is an address that gets filtered, which would cost all
of the above.

Rate-limited per reason **and per subject** (external id, else order, else
variant), with a hard ceiling per hour, so no bug can turn the owner's inbox
into the log file.

---

## 6. Hosting — Railway

See [DEPLOYMENT.md](DEPLOYMENT.md). Relevant here: Railway injects
`RAILWAY_PUBLIC_DOMAIN`, which is what `PUBLIC_BASE_URL` falls back to for
webhook registration and public media URLs, and it blocks SMTP as above.

---

## Authentication summary

| Direction | System | Mechanism |
|---|---|---|
| in | WhatsApp | hex HMAC-SHA256, `X-Hub-Signature-256`, WhatsApp app secret |
| in | Instagram | hex HMAC-SHA256, `X-Hub-Signature-256`, **Instagram** app secret |
| in | Shopify | base64 HMAC-SHA256, `X-Shopify-Hmac-Sha256` + shop-domain check |
| in | Meta media fetcher | HMAC path token over the file path, roots-restricted |
| in | staff | PBKDF2-HMAC-SHA256 password, HMAC-signed session cookie |
| out | Shopify | `X-Shopify-Access-Token` header |
| out | WhatsApp / Instagram | bearer access token |
| out | OpenRouter / Gemini | API key in a **header** |
| out | Resend / Gmail | API key / OAuth refresh token |

**No secret means refuse, never fall back to trusting the caller.** Writing
`if secret and not verify_signature(...)` reads as a convenience for local
development and is in fact the check turning itself off.
