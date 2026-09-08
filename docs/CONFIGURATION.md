# Configuration

Every environment variable the application reads, what it controls, and what
happens when it is missing.

**No secrets appear in this document, in the repository, or in any log.**
`.env.example` holds names and safe placeholders only; `.env` is gitignored
and has never been committed.

All configuration is read in exactly one place — `config/settings.py` — which
is imported by every layer. Nothing has a hardcoded credential, and anything
missing degrades to a documented "not configured" behaviour rather than
raising at import time.

---

## How values are read

| Helper | Behaviour |
|---|---|
| `_bool` | `1/true/yes/on` (case-insensitive) is true; blank falls back to the default |
| `_int` / `_float` | unparseable falls back to the default rather than crashing |
| `_csv` | comma-separated → tuple. An **explicitly empty** variable means "do not constrain this", which is a different answer from unset — the default applies only to the unset case |
| `_first_env` | the first of several names that is actually set |

### Aliases

Several variables accept a second name. The prefixed name is the documented
one (it says which feature the variable belongs to, which matters in a panel
holding fifty of them), but the generic name is what people paste out of a
provider's own instructions, and silently ignoring it would look exactly like
the feature not working.

| Canonical | Also accepted |
|---|---|
| `LLM_MODEL` / `LLM_MEDIA_MODEL` / `LLM_API_KEY` | `GEMINI_MODEL` / `GEMINI_MEDIA_MODEL` / `GEMINI_API_KEY` |
| `SHOPIFY_WEBHOOK_SECRET` | `SHOPIFY_API_SECRET` |
| `ALERT_EMAIL_TO` | `STORE_OWNER_EMAIL` |
| `ALERT_EMAIL_FROM` | `SMTP_FROM`, `ALERT_SMTP_USERNAME`, `SMTP_USER` |
| `ALERT_SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` | `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` |
| `MEDIA_URL_SECRET` | falls back to `DASHBOARD_SESSION_SECRET` |
| `PUBLIC_BASE_URL` | falls back to `RAILWAY_PUBLIC_DOMAIN` (injected by the host) |

---

## Required in production

Five things, without which the system is either insecure or silently broken.

| Variable | Without it |
|---|---|
| `DATABASE_URL` | SQLite in a deployment is **refused at boot** (ephemeral filesystem = all history wiped every redeploy) |
| `WHATSAPP_APP_SECRET` | `/webhooks/whatsapp` refuses **every** delivery with 503 — no customer on that channel is answered |
| `INSTAGRAM_APP_SECRET` | same, for `/webhooks/instagram` |
| `SHOPIFY_WEBHOOK_SECRET` | **no order status push ever fires**, silently — orders stay `Confirmed` forever |
| `DASHBOARD_SESSION_SECRET` | login refuses outright (503); a paused conversation has no way back to the customer |

The last four follow one rule: **no secret means refuse, never fall back to
trusting the caller.**

---

## Database

| Variable | Default | Controls |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./wanas.db` | connection URL. `postgres://` and `postgresql://` are rewritten onto the psycopg3 dialect. |
| `ALLOW_SQLITE_IN_DEPLOY` | off | override the refusal above. Only for someone who genuinely means it. |
| `AUTO_MIGRATE_SCHEMA` | `1` | add columns the models declare and a table lacks, at startup. Additive only. `0` reports the drift and changes nothing. |

## LLM

| Variable | Default | Controls |
|---|---|---|
| `LLM_PROVIDER` | `openrouter` | `openrouter` \| `gemini` \| `fake` |
| `OPENROUTER_API_KEY` | — | OpenRouter key. Deliberately **not** an alias of `LLM_API_KEY`: sharing one hands a routed-inference key to a different vendor, which only surfaces later as an auth failure far from its cause. |
| `LLM_API_KEY` | — | the non-OpenRouter provider's key |
| `LLM_MODEL` | provider default | the conversation model — the tool loop and every sentence a customer reads |
| `LLM_MEDIA_MODEL` | provider default | the model that reads a voice note or photo. A **separate** default because the conversation model may have no audio endpoint at all. |
| `COMMENT_CLASSIFIER_MODEL` | blank | optional cheaper model for comment classification; blank reuses the conversation model |
| `OPENROUTER_PROVIDERS` | `z-ai,deepinfra,novita` | which upstream stacks may serve the model, preferred first. **This is what makes `temperature` a setting rather than a suggestion** — see [INTEGRATIONS.md](INTEGRATIONS.md). Blank keeps the provider's own order *within the filtered set*; it never restores the unfiltered default. |
| `OPENROUTER_QUANTIZATIONS` | `fp8,bf16,fp16` | allowed precisions. `unknown` is excluded on purpose. |
| `LLM_DEBUG_PAYLOAD` | `0` | write every request body to the log. Development only. |

## Channel — WhatsApp

| Variable | Default | Controls |
|---|---|---|
| `WHATSAPP_PHONE_NUMBER_ID` | — | outbound sending |
| `WHATSAPP_ACCESS_TOKEN` | — | outbound sending |
| `WHATSAPP_APP_SECRET` | — | **inbound authentication** (see Required) |
| `WHATSAPP_VERIFY_TOKEN` | — | the `hub.challenge` subscription handshake |
| `WHATSAPP_API_VERSION` | `v21.0` | Graph API version |

> `*_configured` (outbound) and `*_webhooks_configured` (inbound) are
> deliberately different questions. Do not merge them, and do not gate inbound
> on the weaker one.

## Channel — Instagram

| Variable | Default | Controls |
|---|---|---|
| `INSTAGRAM_ACCOUNT_ID` | — | the numeric account id (`?fields=user_id`), not the handle |
| `INSTAGRAM_ACCESS_TOKEN` | — | outbound; refreshed automatically every 60 days |
| `INSTAGRAM_APP_SECRET` | — | inbound authentication. **A different string from the WhatsApp app secret**, even inside the same Meta app. |
| `INSTAGRAM_VERIFY_TOKEN` | — | subscription handshake |
| `INSTAGRAM_APP_SCOPED_ID` | blank | the *other* id the same account is handed out under (`?fields=id`). Optional, and used only to recognise ourselves — matching only one of the two ids is how a bot ends up answering itself in public. |
| `INSTAGRAM_USERNAME` | blank | appears in customer-facing copy only |
| `INSTAGRAM_API_VERSION` | `v23.0` | Graph API version |
| `INSTAGRAM_COMMENTS_ENABLED` | `0` | the **public** comment surface. Off by default so the DM half can ship and be watched before anything is visible to everyone scrolling past. |
| `INSTAGRAM_PUBLIC_REPLY_ENABLED` | `1` | whether a visible reply is written under the comment at all |
| `INSTAGRAM_COMMENTS_DM_ENABLED` | `1` | the private half. Off means every category answers in public only — the setting to reach for if private replies draw a warning, without taking the comment surface down with them. |
| `INSTAGRAM_COMMENT_MAX_AGE_HOURS` | `48` | older comments are ignored (the private-reply window is 7 days, and a reply to a month-old post is noise) |
| `INSTAGRAM_COMMENT_RATE_LIMIT` | `3` | per-commenter cap per rolling hour |
| `INSTAGRAM_FAQ_RATE_LIMIT` | `5` | the same for fixed FAQ answers, counted **separately** — an FAQ reply costs no model call, so it must not spend the budget that exists to stop a flood of DMs |

## Commerce platform — Shopify

| Variable | Default | Controls |
|---|---|---|
| `SHOPIFY_STORE_DOMAIN` | — | e.g. `yourstore.myshopify.com` |
| `SHOPIFY_ADMIN_TOKEN` | — | Admin API access token |
| `SHOPIFY_API_VERSION` | `2026-07` | must not go below `2026-01`, where `inventorySetQuantities` grew its required compare-and-set. Below that, stock writes are refused rather than risking an oversell. |
| `SHOPIFY_VENDOR` | brand name | the vendor on every product this app creates |
| `SHOPIFY_WEBHOOK_SECRET` | — | **inbound authentication** (see Required) |
| `RECONCILE_REPORT_ON_BOOT` | `1` | log which local products the platform no longer has. A report only — the deleting half is a manual script, because a reconcile that deletes unattended is one bad read from an empty catalog. |

## Conversation and memory

| Variable | Default | Controls |
|---|---|---|
| `HISTORY_CAP` | `150` | the **live slice** — what a new turn continues from |
| `MODEL_CONTEXT_MESSAGES` | `24` | the verbatim window sent to the model |
| `MODEL_CONTEXT_RECALL` | `60` | older messages, compacted. `0` turns recall off. |
| `SESSION_ARCHIVE_CAP` | `2000` | the whole stored transcript |
| `SESSION_EXPIRY_HOURS` | `6` | idle before the conversation ends (moves `context_start`; deletes nothing) |
| `TOOL_LOOP_CAP` | `8` | tool rounds per turn |
| `MAX_QUANTITY_PER_LINE` | `10` | cart line cap |
| `MESSAGE_DEBOUNCE_SECONDS` | `6.0` | how long fragments are collected before one turn runs. `0` processes inline in the caller's thread — what the tests want, never what production wants. |
| `MESSAGE_WORKERS` | `8` | how many *different* conversations run at once (one conversation is always serial) |

## Media

| Variable | Default | Controls |
|---|---|---|
| `VOICE_NOTES_ENABLED` | `1` | transcribe voice notes |
| `IMAGE_UNDERSTANDING_ENABLED` | `1` | read photos |
| `IMAGE_MATCH_CONFIDENCE` | `0.6` | below this the reading only informs a better question, it never names a product |
| `INTERACTIVE_MESSAGES_ENABLED` | `1` | tappable pickers; off falls back to asking in prose |
| `MEDIA_URL_SECRET` | `DASHBOARD_SESSION_SECRET` | signs the public media URLs the platform's fetcher uses |
| `PUBLIC_BASE_URL` | `RAILWAY_PUBLIC_DOMAIN` | where this app is reachable, for webhook registration and public media URLs |

## Dashboard

| Variable | Default | Controls |
|---|---|---|
| `DASHBOARD_ENABLED` | `1` | mount the staff dashboard. On by default because it requires a login — forgetting the flag exposes nothing; only forgetting the secret does, and that is refused explicitly. |
| `DASHBOARD_SESSION_SECRET` | — | signs the session cookie (see Required) |
| `DASHBOARD_SESSION_HOURS` | `12` | session lifetime |

## Scheduled work

| Variable | Default | Controls |
|---|---|---|
| `REENGAGEMENT_INTERVAL_SECONDS` | `1800` | the one clock. `<= 0` disables the loop entirely. |
| `ABANDONED_CART_HOURS` | `2.0` | idle before the "still interested?" nudge |
| `ABANDONED_CART_MAX_AGE_HOURS` | `48.0` | past this a cart is dead, not abandoned — an ancient test cart must not be nudged forever |
| `WEBHOOK_EVENT_RETENTION_DAYS` | `30` | how long an idempotency claim is kept before pruning. **Must stay above the longest platform retry window** (days for Meta, 48h for Shopify) — pruning earlier re-opens the duplicate-order door. `0` disables the pass. |

## Proactive message templates

Outside the platform's 24-hour customer-service window, free-form
business-initiated text is refused. These name approved templates; **blank
means that message type cannot be delivered outside the window** — the line is
stored `delivered=False` and a staff alert is raised.

`WHATSAPP_TEMPLATE_BACK_IN_STOCK`, `WHATSAPP_TEMPLATE_ABANDONED_CART`,
`WHATSAPP_TEMPLATE_ORDER_UPDATE`, `WHATSAPP_TEMPLATE_FEEDBACK_REQUEST`,
`WHATSAPP_TEMPLATE_ORDER_CONFIRMATION`, `WHATSAPP_TEMPLATE_LANGUAGE`
(default `ar`).

`ORDER_UPDATE` is the one that matters most: a customer orders, stops writing,
and the fulfilment happens a day or two later — outside the window by
construction. `ORDER_CONFIRMATION` exists for completeness; the confirmation
is always sent seconds after the customer's own message.

## Owner alerts

| Variable | Default | Controls |
|---|---|---|
| `ALERT_EMAIL_TO` | — | recipient. Blank = no email at all (a documented off state; the queue and dashboard still carry every alert). |
| `RESEND_API_KEY` | — | **preferred transport.** HTTPS/443, nothing in it that expires. |
| `RESEND_FROM` | `onboarding@resend.dev` | must be a domain verified with Resend; the default delivers only to the Resend account owner — who is who these alerts go to anyway. |
| `GMAIL_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN` | — | second transport. All three or none. The refresh token dies on a password change, a revoked grant, or after 7 days if the consent screen is in Testing. |
| `ALERT_SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` | `smtp.gmail.com` / `587` | third transport. **Cannot deliver from Railway**, which blocks every outbound SMTP port; kept for local use and hosts that permit it. |
| `ALERT_EMAIL_FROM` | the sending mailbox | most providers will not let you forge this |
| `ALERT_EMAIL_COOLDOWN_SECONDS` | `900` | how long the same reason about the same subject stays quiet |
| `ALERT_EMAIL_MAX_PER_HOUR` | `20` | a hard ceiling, so no bug can turn the owner's inbox into the log file |

## Development and debugging — must be OFF in production

| Variable | Default | Controls |
|---|---|---|
| `CHATBOT_DEBUG` | `0` | surfaces **raw provider errors in the customer's reply**. Logged loudly at every boot if on. |
| `HARNESS_ENABLED` | `0` | mounts an **unauthenticated** local chat UI at `/harness`. Anyone who can reach it can converse as any customer identity. It ships off precisely so that forgetting a variable is not what exposes it. |
| `LLM_DEBUG_PAYLOAD` | `0` | every request body into the log |
| `WANAS_TEST_DATABASE_URL` | — | the **only** way to point the test suite at PostgreSQL. A plain `DATABASE_URL` is ignored by the suite, because the fixtures drop schemas. |
| `RUN_LIVE_TESTS` | — | `1` runs the tests that hit a real model and cost quota |

---

## Verifying a deployment's configuration

`GET /health` answers what is wired up and what is still waiting on a
credential, without revealing any value:

```json
{
  "status": "ok",
  "llm_provider": "...", "llm_key_set": true,
  "whatsapp_configured": true,  "whatsapp_webhooks_configured": true,
  "instagram_configured": true, "instagram_webhooks_configured": true,
  "instagram_comments": true,   "instagram_token_expires_at": "...",
  "shopify_configured": true,   "shopify_webhooks_configured": true,
  "voice_notes": true, "image_understanding": true,
  "dashboard_configured": true,
  "alert_email_configured": true, "alert_email_transport": "resend",
  "catalog_products": 19, "catalog_variants": 211
}
```

`catalog_products` / `catalog_variants` are there for the same reason as
everything else on that page: an unseeded database behind a perfectly healthy
process looks identical to "working" from the outside, and costs a customer
conversation to notice.

Boot also logs, loudly, every configuration state that is silently expensive:
a channel configured for sending but not for authenticated inbound, a missing
webhook secret, an alert transport that cannot deliver from this host, the
debug flags, and the harness.
