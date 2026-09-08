# Troubleshooting

Symptom → cause → what to look at → what to do.

Ordered by how expensive the failure is, not by how likely. The first three
sections are silent failures: the process is healthy, `/health` may say
everything is configured, and nothing looks wrong from the outside.

**First stop, always:** `curl https://<domain>/health`, then read **stderr**
in the deploy log. Ordinary logging goes to stdout and only WARNING and above
to stderr, so anything in stderr is something to look at.

---

## Silent total failures

### Customers message and never get a reply

| Check | If |
|---|---|
| `/health` → `whatsapp_webhooks_configured` / `instagram_webhooks_configured` | **false** → the app secret is missing. The endpoint refuses **every** delivery with 503, by design: a webhook whose signature cannot be checked must never be trusted. Set it. |
| stderr: `rejected a webhook with a bad signature` | the configured app secret does not match the one the platform is signing with. Rotate/copy it again — and note the two channels use **different** secrets even inside the same app. |
| stderr: `no inbound message extracted from a … delivery` | deliveries are arriving and authenticating, but this adapter took nothing out of them. A payload shape changed, or the subscription is for the wrong field. |
| stderr: `dropping inbound message: conversation … is paused` | that conversation is waiting on a person. Open the dashboard and release or reply to it. |
| the platform's own webhook config | the subscription may have been switched off after repeated timeouts. Re-subscribe. |
| nothing at all in the log | deliveries are not reaching the app. Check the callback URL and that the domain resolves. |

### Orders never move past "Confirmed" — no shipping notification ever

`/health` → `shopify_webhooks_configured`. **False is the whole answer**: no
signing secret means the status webhook refuses everything, so fulfilments
never reach the customer and orders stay `Confirmed` forever. It is warned at
every boot for exactly this reason.

If it is true: check the subscriptions exist (they are registered
automatically at boot when a public URL is configured), and look for
`rejected a Shopify webhook with a bad signature` or `ignoring a Shopify
webhook for '<domain>'` — the latter means a correctly signed delivery for a
*different* store, which is refused because applying it would move the wrong
orders.

### Every search says "we don't have that"

`/health` → `catalog_products` / `catalog_variants`. Zero means an unseeded
database behind a perfectly healthy process. Startup auto-seeds when the
catalog is empty; if it did not, look for `could not auto-seed the catalog` or
`seed data failed its own consistency check`, then run `python manage.py seed`
against the right database.

Non-zero but a specific product is missing: it was probably created in the
platform's own admin and not mirrored. The importer runs at boot **and on
every scheduler tick**; look for `catalog import:` lines. A product still
wearing the platform's `Default Title` placeholder is **skipped on purpose** —
that is a half-made product, and mirroring it writes a phantom "One Size" row
at 0.00.

### The whole history disappeared after a deploy

`DATABASE_URL` is pointing at SQLite inside a deployment. The container's
filesystem is ephemeral: sessions, clients, orders and queue rows are wiped on
every redeploy while the startup seed refills catalog and fees, so the shop
looks alive with nobody's history in it.

This is refused at boot now — unless `ALLOW_SQLITE_IN_DEPLOY=1` is set. Look
for `Refusing to start: DATABASE_URL resolves to SQLite` or the
`ALLOW_SQLITE_IN_DEPLOY=1` warning. Point it at PostgreSQL.

---

## Orders

### "The order failed" but the order exists in the platform's admin

Look for:

```
order …: the local write failed at stage=<stage> after Shopify created <name>
order …: refusing the order; Shopify order <name> was cancelled and its stock returned
```

`stage` names the step. **If the second line says `LEFT OPEN -- cancel it by
hand`, do that now** — the sale exists remotely and its stock is gone, with
nothing on this side that knows about it.

The most likely cause of a `local_write` failure is a missing column. Check
the boot log for schema drift, and run
`python scripts/migrate_schema.py --apply`. This exact failure once cost four
days of orders.

### Every order placement fails at the last step

Almost certainly a SQL construct that works in SQLite and not in PostgreSQL.
Run the suite against PostgreSQL — see [TESTING.md](TESTING.md).

### Orders are being refused with `store_unavailable`

`Refusing an order: Shopify unreachable` in stderr. The order path **refuses
rather than degrades**, on purpose: an order written locally while the
platform does not know about it is stock sold twice, and the customer only
finds out when nothing arrives. Browsing still works from local numbers.

Check the platform's status, the admin token, and whether the throttle floor
is being hit regularly (that is the signal to move to a webhook-driven cache).

### A customer was told "sold out" for something in stock

Something read `variants.stock_qty` instead of the live overlay. That column
is a seeded value nothing keeps current. Everything deciding whether a sale
may happen must go through `catalog.live_stock(variant)`.

This one compounds: a refusal joins the stock waitlist, so half an hour later
the customer is told the item is "back in stock" when it never left.

### A customer got a "back in stock" message for something never out of stock

`StockWaitlistEntry.observed_stock` was NULL or positive. The message claims
an *event*, so it goes out only where both ends are on record: at or below
zero then, above zero now. Entries with no baseline are baselined silently.
Look for `waitlist entry … was created against a non-zero level`.

---

## Messages that do not arrive

### A confirmation or status update is in the dashboard but not on the phone

Check the message's `delivered` flag and the staff queue. Outside the
platform's 24-hour customer-service window, free-form business-initiated text
is **refused by the platform**, which is routine for a parcel that ships the
next day.

The line is stored `delivered=False` and a staff alert is raised. The fix is
an approved template: set the matching `WHATSAPP_TEMPLATE_*` variable.
`ORDER_UPDATE` is the one that matters most.

### Instagram images never appear

Outbound images on that channel must be **public HTTPS URLs** — the platform
fetches them itself, and it has no session cookie. Local files go through the
HMAC-gated `/public/media/...` route, which needs `PUBLIC_BASE_URL` (or the
host's injected domain) *and* a signing secret. With neither, `public_url_for`
returns `None` and the send is recorded as not delivered.

`data/inbound` is unreachable through that route **even with a correctly
computed token** — those are customers' own photos and voice notes.

### The Instagram channel went completely quiet after about two months

The 60-day token expired. `/health` → `instagram_token_expires_at`. Look for
`instagram token refresh failed`. Mint a new long-lived token and set it; the
scheduler keeps it going afterwards, and that field is what makes a broken
refresh visible weeks early.

---

## The model

### Replies are occasionally garbled or answer a question nobody asked

The classic cause is provider routing. One model id is hosted on many upstream
stacks, and by default a stack that does not implement `temperature` is sent
it anyway and **silently drops it** — so a reply meant to be sampled at 0.3
was generated at that stack's default. It degrades *per request*: most fine,
the occasional one broken.

Check `OPENROUTER_PROVIDERS` and `OPENROUTER_QUANTIZATIONS` are set. The
`require_parameters` filter they turn on is the part that matters.

The second cause is dropped reasoning blocks: without `reasoning_details`
handed back with the assistant turn, the model gets its tool results back with
no record of *why* it asked for them.

### Replies stop mid-sentence

`finish_reason` hit the ceiling. The guard regenerates twice asking for
shorter and then sends `TRUNCATED_FALLBACK` — so seeing an actual fragment
means the guard is not running. Check `agent.py`'s truncation handling and
that the provider is reporting `finish_reason` at all.

### The bot promises to check something and then says nothing

A dangling promise: a reply with no tool calls **is** the final answer as far
as the loop is concerned, and nothing in this system ever wakes up to finish a
promise. The guard retries twice and then sends a fallback that *asks a
question* instead. If promises are reaching customers, the detection pattern
needs the new wording added.

### The bot forgot a product discussed ten minutes ago

`HISTORY_CAP` and `MODEL_CONTEXT_MESSAGES` are different questions. The cap
counts **messages**, and one exchange costs four to six of them once tool
calls and results are counted. Raise `MODEL_CONTEXT_RECALL` rather than the
cap. **Never** add summarisation — compaction removes whole messages only, so
every sentence the model reads is the exact sentence that was said.

### The bot invented a product's colours after saying it could not find it

A tool refusal without `product_in_conversation` attached. The model
reconstructs an id it can no longer see. `catalog_tools._not_found` must
attach the fact so the model can call the tool *again* — never substitute an
id behind the model's back.

### Voice notes always go to a person

The configured media model has no audio endpoint (`no endpoints found that
support input audio`). Set `LLM_MEDIA_MODEL` to one that does — this is
exactly the bug that variable exists for. A truncated transcription is
deliberately returned as `""`, because half a voice note does not read as
broken, it reads as a *shorter message*, and the whole turn is built on it.

---

## Dashboard

| Symptom | Cause |
|---|---|
| Login returns 503 | `DASHBOARD_SESSION_SECRET` unset. With no secret a signed cookie could never be told apart from a forged one, so login refuses rather than signing something guessable. |
| Correct password rejected | the account is deactivated, or the hash predates a change. `python manage.py create-staff` for a fresh one. |
| A section 403s but its nav item is visible | it should not be — the sidebar hides what an account cannot open. The 403 is the control working; the nav is the bug. |
| Everyone can see everything | accounts with a NULL `role` read as **owner**. That is deliberate: the opposite locks everyone out of the screen that hands permissions out. Assign roles explicitly. |
| A conversation is stuck "paused" | that is a handoff waiting for a person. Reply or release it. Every handoff also raises an owner email. |

---

## Alerts and email

| Symptom | Cause |
|---|---|
| No alert emails at all | `/health` → `alert_email_configured`. Needs a recipient **and** a transport. |
| `alert_email_transport: "smtp"` in production | **SMTP cannot deliver from this host** — every outbound SMTP port is blocked; the container gets "Network is unreachable" while plain HTTP connects instantly. Set `RESEND_API_KEY`. This is warned at boot. |
| Emails stopped after working for a week | a Gmail OAuth refresh token expired — a password change, a revoked grant, or a consent screen left in Testing (7 days). This is why Resend is preferred: nothing in it expires. |
| Too many emails | they are rate-limited per reason **and** per subject, with a ceiling per hour. If the volume is real, the queue is the record and the email is only the tap on the shoulder. |
| No email for a confirmed order | deliberate. Confirmations fire on every sale, and an address carrying those gets filtered — which would cost every alert that actually needs a person. |

---

## Performance

| Symptom | Cause |
|---|---|
| Replies take 30s+ | the tool loop is doing several model round trips. Check `TOOL_LOOP_CAP` and whether the model is looping on a refusal. |
| Webhook timeouts | work has moved back into the endpoint. It must claim, record and return 200 — the turn runs on a worker thread. The endpoint is `async` and the turn is not, so one message would block every other conversation. |
| Platform throttling | the client tracks throttle status and slows below a floor. Hitting it regularly is the signal to move to a webhook-driven cache. |
| Memory grows for weeks | historically a per-conversation lock leak (fixed: refcounted) or `webhook_events` growing forever (fixed: retention). Check `WEBHOOK_EVENT_RETENTION_DAYS` is not `0`. |
| Three replies to one typed sentence | `MESSAGE_DEBOUNCE_SECONDS` is `0`. That is the test setting. |

---

## Deploys

| Symptom | Cause |
|---|---|
| Deploy fails at the healthcheck | `/health` did not answer 200 in time — usually the database is unreachable. **The previous container keeps serving**, which is the point. Fix the cause; do not remove the healthcheck. |
| Crash loop on `ImportError: psycopg2` | a bare `postgres://` URL that was not rewritten. `domain/db.py` handles both spellings; check nothing else parses the URL. |
| Boot says `could not reconcile the database schema` | drift needs a person. Run `python scripts/migrate_schema.py` (dry run) and read it. A `NOT NULL` column with no default is **reported, never guessed at**. |
| Buffered messages lost on every deploy | one channel's `dispatcher.shutdown(wait=True)` is missing from the lifespan. Both are required. |
| A behaviour changed with no code change | the interpreter or a dependency moved. Both are pinned (`.python-version`, `requirements.txt`) precisely so this cannot happen silently. |

---

## Diagnostic commands

```bash
curl -s https://<domain>/health | python -m json.tool

# The three signature gates, from outside. Expect 403, 403, 401.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<domain>/webhooks/whatsapp  -d '{}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<domain>/webhooks/instagram -d '{}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<domain>/webhooks/shopify   -d '{}'

# These must be 404 and 401 respectively.
curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/harness
curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/dashboard/api/conversations
```

```bash
python scripts/shopify_check_live.py             # read-only smoke check
python scripts/migrate_schema.py                 # dry run: what drifted
python scripts/shopify_reconcile_products.py     # dry run: what is stale
```

**Never** paste a credential into a log, an issue, or a chat while
diagnosing — including "just the first few characters".
