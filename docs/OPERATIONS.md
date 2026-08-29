# Operations

Running this thing: what to set, what to register, and what to check when
something looks wrong.

## Before a real customer can reach it

- [ ] `DATABASE_URL` points at PostgreSQL (`postgresql+psycopg://...`)
- [ ] `HARNESS_ENABLED` is **unset or 0** — it ships off; setting it to 1 in a
      reachable deployment exposes a chat UI that can converse as any customer
- [ ] `CHATBOT_DEBUG=0` — raw provider errors must never reach a customer reply
- [ ] `LLM_PROVIDER=openrouter` and `OPENROUTER_API_KEY` set (or
      `LLM_PROVIDER=gemini` with `LLM_API_KEY`/`GEMINI_API_KEY`) — otherwise
      the rehearsal stand-in answers, and it is a keyword matcher, not the
      product. Under the default OpenRouter provider that one key covers
      everything: chat, voice-note transcription and photo reading all run on
      the same model; under `LLM_PROVIDER=gemini`, voice and photos both run
      on the Gemini key instead
- [ ] `SHOPIFY_STORE_DOMAIN` + `SHOPIFY_ADMIN_TOKEN` set, with the token's
      scopes covering **both** the chat path and the dashboard's Shopify
      section: `read_products`, `read_inventory`, `read_locations`,
      `read_orders` for the bot; `write_products`, `write_inventory`,
      `write_orders`, `write_fulfillments`, `read_customers` for the
      dashboard's product/order management, plus
      `read_merchant_managed_fulfillment_orders` and
      `read_assigned_fulfillment_orders`, which `read_orders` does **not**
      imply and which the Ship button cannot work without -- the order drawer
      says so in place of the button, and `fulfill` refuses with
      `fulfillment_scope_missing`, rather than either one failing as an
      outage. A missing write scope shows up as a `store_unavailable`
      (config) refusal from the dashboard action that needed it, not a crash
- [ ] `SHOPIFY_WEBHOOK_SECRET` set — without it orders stay `Confirmed`
      forever and no tracking message ever goes out. The subscriptions
      themselves register automatically on boot once `SHOPIFY_STORE_DOMAIN`
      and a public URL (`PUBLIC_BASE_URL`, or `RAILWAY_PUBLIC_DOMAIN` which
      Railway sets for you) are both configured -- see "Registering the
      webhooks" below for the one step that still has to be done by hand
- [ ] Meta webhook registered and verified
- [ ] A real shipping fee set for every governorate you deliver to
      (`python manage.py set-fee`) — an order for an unpriced governorate
      is refused, on purpose
- [ ] SKUs linked (`python scripts/shopify_set_skus.py --apply`), then
      `python scripts/shopify_check_live.py` reports no disagreement
- [ ] Meta templates approved for the proactive messages (order confirmation,
      status pushes, feedback request, back-in-stock, abandoned-cart nudge).
      Until then they go out as free-form text, which only works for verified
      test recipients and inside the 24-hour window; outside it,
      `WHATSAPP_TEMPLATE_BACK_IN_STOCK` / `WHATSAPP_TEMPLATE_ABANDONED_CART`
      unset means that message queues a staff alert instead of going out
      automatically (`domain/services/notifications.py::send_proactive`)
- [ ] `DASHBOARD_SESSION_SECRET` set (a long random string) **and** at least
      one staff account exists (`python manage.py create-staff`) — without
      the secret, `/dashboard` cannot log anyone in, and a conversation that
      pauses for a person has no way back to the customer

`GET /health` answers most of this:

```json
{
  "status": "ok",
  "llm_provider": "openrouter",
  "llm_key_set": true,
  "whatsapp_configured": true,
  "instagram_configured": false,
  "instagram_comments": false,
  "instagram_token_expires_at": null,
  "shopify_configured": true,
  "shopify_webhooks_configured": true,
  "voice_notes": true,
  "image_understanding": true,
  "dashboard_configured": true
}
```

## Launching the Instagram channel

The Instagram build is inert until credentials exist: `instagram_configured`
false on `/health`, the webhook refuses with 503, and outbound is logged
instead of sent. Nothing below is needed for the WhatsApp channel to keep
working.

**Meta side, once:**

- [ ] The shop's Instagram account is **Professional** (Business or Creator),
      and Settings → Messages and story replies → Allow access to messages is
      ON — without that the webhook never fires no matter what the app says.
- [ ] Add the **Instagram product** to the existing Meta app (one app, two
      products). With the Instagram Login flavour the host is
      `graph.instagram.com` and the signing secret is the **Instagram app
      secret — a different string from `WHATSAPP_APP_SECRET`**, even inside
      the same app. Do not "simplify" one away.
- [ ] Generate a long-lived **Instagram User Access Token**; note the numeric
      professional-account ID (`INSTAGRAM_ACCOUNT_ID`, not the @handle), the
      secret, a random verify token, and the @handle (`INSTAGRAM_USERNAME`,
      used in copy only).
- [ ] Permissions: `instagram_business_basic`,
      `instagram_business_manage_messages`, `instagram_business_manage_comments`.
      Advanced Access is required to serve accounts other than your own — i.e.
      required at launch, not while testing against the shop's own account.
- [ ] Webhook callback `{PUBLIC_BASE_URL}/webhooks/instagram`; subscribed
      fields exactly: `messages`, `messaging_postbacks`, `messaging_seen`,
      `comments`. **Do not subscribe `message_echoes`.**

**Deploy sequence:** ship with comments OFF and no credentials, watch
WhatsApp is unaffected, then add credentials and verify the handshake from
Meta's dashboard. DM the shop from a personal account; check reply,
`mark_seen`, dashboard badge. Send a photo, a voice note and a forwarded
reel. Place one real order end to end and confirm the confirmation arrives
in the DM. Only after days of quiet alerts: turn comments on.

**The 60-day token — what its failure looks like.** Instagram long-lived
tokens expire 60 days after issuance. There is no refresh-token flow; the
token refreshes itself via `graph.instagram.com/refresh_access_token` while
still valid. When it dies the symptom is *silence*: no crash, no failed
deploy, just the webhook never being answered and 190-series auth errors in
the logs. `integrations/instagram/token.py` refreshes automatically when
the stored expiry is within ten days (scheduler tick + boot, rate-limited to
daily), stores the new token in `integration_tokens` — which then wins over
the env var — and enqueues an `instagram_token_refresh_failed` alert when it
cannot. `/health`'s `instagram_token_expires_at` should always be ~50–60 days
out; if it stops moving, fix the refresh before it hits zero.

**Turning comments off in a hurry:** set `INSTAGRAM_COMMENTS_ENABLED=0` and
redeploy. The DM half keeps working; only the public surface goes dark.
Comments also ship with a per-commenter rate limit and drop the shop's own
comments before anything else runs — but if the bot is ever seen replying to
itself under a post, that flag is the kill switch.

## Registering the webhooks

**Meta → this app.** Callback URL `https://<host>/webhooks/whatsapp`, verify
token = `WHATSAPP_VERIFY_TOKEN`. Subscribe to the `messages` field. The
handshake is a `GET`; the app answers it only when the token matches.

**Shopify → this app.** Callback URL `https://<host>/webhooks/shopify`,
format JSON, four topics:

| Topic | What it does here |
| --- | --- |
| `orders/fulfilled` | Order → `Shipped`, customer gets "طلبك في الطريق ليك 🚚" |
| `orders/partially_fulfilled` | Order → `Packed` |
| `fulfillments/update` | Order → `Delivered` when Shopify says `delivered` |
| `orders/cancelled` | Order → `Cancelled`, stock returned locally |

**The subscriptions register themselves.** `app.py`'s
`_register_shopify_webhooks` (`integrations/shopify/webhook_registration.py`) runs on
every boot once `SHOPIFY_STORE_DOMAIN`/`SHOPIFY_ADMIN_TOKEN` and a public URL
are set, checks what is already subscribed against this exact callback URL,
and creates whatever is missing — idempotent, safe to leave running
permanently, nothing to click through in Shopify Admin for this part.

**The signing secret is the one manual step left, and it cannot be
automated:** every delivery is HMAC-verified against
`SHOPIFY_WEBHOOK_SECRET`; with no secret configured the endpoint refuses
everything, because an unauthenticated way to cancel orders is worse than no
integration at all. For a webhook subscription created the way this app
creates it — through the Admin API, using a custom app's own credentials —
the matching secret is that app's **API secret key**, not the separate
per-store key shown on the old Settings → Notifications → Webhooks page.
Find it in Shopify Admin → **Settings → Apps and sales channels → Develop
apps → (this app) → API credentials → API secret key** (sometimes labelled
"Client secret"). Copy it into `SHOPIFY_WEBHOOK_SECRET` in Railway and
redeploy or restart — no code change needed, and `/health`'s
`shopify_webhooks_configured` flips to `true` the moment it takes effect.

## The staff dashboard

`https://<host>/dashboard` — log in with a staff account, see who is waiting
on a reply, and answer them. It exists because `request_human` has always
paused a conversation and queued it; for a long stretch nothing read that
queue back or un-paused the conversation.

```bash
python manage.py create-staff <username>                          # an owner
python manage.py create-staff <username> --role staff --can inbox,orders
```

Both prompt for a password twice. The default is `--role owner` on purpose:
the first account on a fresh database has to be able to open **الفريق** and
scope everyone else.

### Roles and permissions

Two roles. An **owner** sees everything, including the Team section that hands
permissions out. A **staff** member sees only the sections ticked for them —
one permission per section (`inbox`, `orders`, `products`, `inventory`,
`collections`, `customers`, `queue`, `analytics`, `settings`, `manage_staff`;
the list lives in `domain/services/staff_admin.py`).

After the first owner exists, everything else is done from the dashboard:
**الفريق** adds an account, changes its role or its ticks, resets its
password, and deactivates it. Deactivate rather than delete someone who
leaves, so their name stays attached to what they already resolved.

Two things the UI will refuse, both of them "you cannot lock the shop out of
itself": the last active owner cannot be demoted or deactivated, and nobody
can edit their own role or permissions.

An account created **before** permissions shipped has no role stored and is
read as an owner. That is deliberate — the alternative is a deploy in which
every existing login is scoped to nothing and nobody can reach the screen that
would fix it. Give those accounts an explicit role from the dashboard when you
next touch them.

Permissions are enforced on the endpoints (`dashboard/guard.py`), not by
hiding nav items. The sidebar hiding a section is a courtesy; the route behind
it refuses on its own with a 403.

### Language

The dashboard reads Arabic or English, switched by the **AR / EN** button at
the bottom of the sidebar. It is a per-browser preference (`localStorage`,
key `wanas.lang`), not an account setting, and the login page reads the same
key so the language does not flip on the way in. Switching reloads the page:
several label tables are built once at load, and re-rendering would leave
those in the old language.

Arabic is the source language. An untranslated phrase falls back to Arabic
rather than to a blank, and `pytest tests/test_dashboard_i18n.py` fails if any
phrase on the page has no English entry — so adding a screen means adding its
translations in the same commit.

Two things never translate, on purpose: the shop's name ("Wanas Gallery" in
both languages) and the canned quick replies in the conversation composer,
which are sent to customers.

### The logo

`dashboard/wanas.webp`, served at `/dashboard/logo.webp`. Replace the file and
redeploy; nothing in the HTML names it twice.

`DASHBOARD_ENABLED=0` removes the router entirely; leaving it on with no
`DASHBOARD_SESSION_SECRET` set is also safe (login refuses, 503) but means
nobody can reply, which is the situation the checklist above exists to catch
before a launch rather than after the first stuck customer.

## Tuning inbound handling

| Setting | Default | What it changes |
| --- | --- | --- |
| `MESSAGE_DEBOUNCE_SECONDS` | `6` | How long fragments of one message are collected before the agent runs. `0` answers each message on arrival, in the request thread — the test setting, never production. |
| `MESSAGE_WORKERS` | `8` | How many *different* conversations run at once. One conversation is always answered serially. |
| `VOICE_NOTES_ENABLED` | `1` | Off sends every voice note to a person. |
| `IMAGE_UNDERSTANDING_ENABLED` | `1` | Off sends every photo to a person. |
| `IMAGE_MATCH_CONFIDENCE` | `0.6` | The dial between "the bot guesses" and "the bot asks". Raise it if customers are being shown the wrong product; lower it if it keeps asking about photos it clearly recognised. |
| `INTERACTIVE_MESSAGES_ENABLED` | `1` | Off asks for the governorate in prose instead of sending a tappable list. |
| `LLM_MEDIA_MODEL` | blank | A separate model for reading photos, honoured only by `LLM_PROVIDER=gemini` (blank lets Gemini pick its own). Under the default OpenRouter provider it does nothing: chat, voice notes and photos all run on the one conversation model (`LLM_MODEL`, else the pinned default). |

## Reading the logs

The logger names say where you are: `wanas.runtime`, `wanas.agent`,
`wanas.tools`, `wanas.dispatcher`, `wanas.media`, `wanas.channel.whatsapp`,
`wanas.webhooks.shopify`, `wanas.shopify`, `wanas.provider.openrouter`,
`wanas.provider.gemini`, `wanas.dashboard`.

Lines worth alerting on:

| Log line | What it means |
| --- | --- |
| `stripped a file path / tool call from a reply` | The prompt or a tool description has drifted. The sanitiser caught it, but this is the earliest signal. |
| `Refusing an order: Shopify unreachable` | Orders are being refused. Real revenue, right now. |
| `N Shopify variants have no SKU` | Run `scripts/shopify_set_skus.py --apply`; those variants cannot be sold. |
| `tool loop cap hit` | The model is looping instead of answering. |
| `rate limited or out of quota on model` | Provider quota (OpenRouter's shared model, or Gemini under the alternate provider). Customers are seeing "الضغط عالي شوية". |
| `CHATBOT_DEBUG is ON` | Someone shipped a development `.env`. |
| `SHOPIFY_WEBHOOK_SECRET is not set` | Tracking messages will never fire, silently. |
| `failed to handle buffered messages` | A worker swallowed an exception; a customer got no reply. |

## Common situations

**"The bot says everything is sold out."** Check `shopify_check_live.py`. Most
likely the SKUs are not linked, so the live lookup finds nothing and every line
reads as unavailable.

**"A customer says they never got a confirmation."** Look for
`confirmation_delivery_failed` in the staff queue — a failed WhatsApp send
writes one, because with no email there is nothing else that would reach them.

**"An order shipped but the customer was never told."** Either the Shopify
webhook is not registered, or `SHOPIFY_WEBHOOK_SECRET` is wrong. Both show as
silence, not as an error.

**"Replies take forever."** `MESSAGE_DEBOUNCE_SECONDS` is added to every reply
by design. Beyond that, look at the provider timeout and whether the Shopify
snapshot is timing out at eight seconds per turn.

**A conversation has gone silent.** It is probably paused on a handoff — check
`/dashboard` (see "The staff dashboard" above); it lists exactly these, oldest
wait first. Reply there, or resolve without a reply if it turns out to be a
false alarm. Only reach for the database directly
(`channel_identities.paused_until_staff_reply`) if the dashboard itself is the
thing not working.

## Database

Tables are created at startup from the models (`Base.metadata.create_all`).
That adds missing *tables*, never missing *columns* on an existing one — which
is why startup then reconciles the columns too (`_ensure_schema_columns` in
`app.py`, over `domain/schema_drift.py`): additive, idempotent, and logged as

    WARNING wanas.schema: SCHEMA: added missing column orders.source_external_id

Anything it cannot add safely — a `NOT NULL` column with no server default, on
a table that already has rows — is logged as `SCHEMA DRIFT: …` for a person to
resolve, never guessed at. `AUTO_MIGRATE_SCHEMA=0` turns the repair off and
leaves only the report; `python scripts/migrate_schema.py` then shows the
`ALTER TABLE` statements it would run (`--apply` runs them).

**This is what "the bot creates an order and Shopify cancels it" means.** A
column the code writes and the table lacks fails every `INSERT` into that
table; the order path had already created the sale on Shopify by then, so the
compensating cancel fires and the customer is told the shop had a technical
problem. Check the boot log for `SCHEMA DRIFT` first. Seed a fresh database
with `python manage.py seed`.

### Database durability

**`DATABASE_URL` must be PostgreSQL in production**
(`postgresql+psycopg://user:password@host:5432/db`). Railway's bare
`postgres://…` / `postgresql://…` URLs are rewritten onto the psycopg 3
dialect automatically at engine creation (`domain/db.py`) — nothing to
convert by hand, and the URL itself is never logged, only its scheme.

A deployed container's disk is ephemeral: **SQLite there means every session,
client, order and queue row is wiped on the next redeploy**, while the
startup seed refills catalog and shipping fees — so the shop looks alive with
nobody's history in it. That is exactly how all chat history vanished once.
The app therefore **refuses to start** when it finds itself in a deployment
(any of `RAILWAY_ENVIRONMENT` / `RAILWAY_PROJECT_ID` /
`RAILWAY_PUBLIC_DOMAIN` set) and the resolved URL is still `sqlite:`. The fix
is the first checklist item above: set `DATABASE_URL` to PostgreSQL and
redeploy. If you genuinely mean to run SQLite in a container (you almost
certainly do not), `ALLOW_SQLITE_IN_DEPLOY=1` lifts the refusal. Either way,
a WARNING is logged at boot whenever SQLite backs the engine, because its
data is not durable.

**The test suite drops and recreates the whole schema on every pytest run**,
so by default it cannot point anywhere but its own throwaway
`test_wanas.db`: `tests/conftest.py` overwrites `DATABASE_URL`, ignoring any
exported value, and a guard in front of the drop refuses to run unless the
engine provably points at that file. To run the suite against PostgreSQL
deliberately — worth doing before deploying, since
`tests/test_order_transaction.py` depends on it most — set the opt-in
variable:

```bash
WANAS_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/wanas make test
```

An ambient `DATABASE_URL` is never picked up either way; the guard likewise
refuses any target that is neither the suite's file nor the deliberately
named one (and refuses a production-looking database name even then). It
raises a clear error instead of dropping someone's data.

## Maintenance scripts

All of them are dry-run by default, idempotent, and need `--apply`:

```bash
python scripts/shopify_check_live.py    # read-only: do the two sides agree?
python scripts/shopify_set_skus.py      # link local variant_id -> Shopify SKU
python scripts/shopify_sync.py          # reconcile the catalog
```

## Product photos

`data/images/` is what this sample shipped with — a real deployment does not
need to keep bundling photos in the repo. Attach a photo to a product or
variant in Shopify Admin the normal way; the next message that reads that
variant already picks it up (`shopify_catalog.fetch_all` reads it alongside
price and stock, same call, no extra step and nothing to run). It only takes
over from the local file for the colour it was set on — a product with no
photo on Shopify yet keeps serving `data/images/` exactly as before. See
`docs/ARCHITECTURE.md` ("Shopify owns price, stock and orders").
