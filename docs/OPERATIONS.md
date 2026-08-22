# Operations

Running this thing: what to set, what to register, and what to check when
something looks wrong.

## Before a real customer can reach it

- [ ] `DATABASE_URL` points at PostgreSQL (`postgresql+psycopg://...`)
- [ ] `HARNESS_ENABLED` is **unset or 0** — it ships off; setting it to 1 in a
      reachable deployment exposes a chat UI that can converse as any customer
- [ ] `CHATBOT_DEBUG=0` — raw provider errors must never reach a customer reply
- [ ] `LLM_PROVIDER=gemini` and `LLM_API_KEY` set (otherwise the rehearsal
      stand-in answers, and it is a keyword matcher, not the product)
- [ ] `SHOPIFY_STORE_DOMAIN` + `SHOPIFY_ADMIN_TOKEN` set, with the token's
      scopes covering **both** the chat path and the dashboard's Shopify
      section: `read_products`, `read_inventory`, `read_locations`,
      `read_orders` for the bot; `write_products`, `write_inventory`,
      `write_orders`, `write_fulfillments`, `read_customers` for the
      dashboard's product/order management. A missing write scope shows up
      as a `store_unavailable` (config) refusal from the dashboard action
      that needed it, not a crash
- [ ] `SHOPIFY_WEBHOOK_SECRET` set — without it orders stay `Confirmed`
      forever and no tracking message ever goes out. The subscriptions
      themselves register automatically on boot once `SHOPIFY_STORE_DOMAIN`
      and a public URL (`PUBLIC_BASE_URL`, or `RAILWAY_PUBLIC_DOMAIN` which
      Railway sets for you) are both configured -- see "Registering the
      webhooks" below for the one step that still has to be done by hand
- [ ] Meta webhook registered and verified
- [ ] A real shipping fee set for every governorate you deliver to
      (`python -m backend.cli set-fee`) — an order for an unpriced governorate
      is refused, on purpose
- [ ] SKUs linked (`python scripts/shopify_set_skus.py --apply`), then
      `python scripts/shopify_check_live.py` reports no disagreement
- [ ] Meta templates approved for the proactive messages (order confirmation,
      status pushes, feedback request, back-in-stock, abandoned-cart nudge).
      Until then they go out as free-form text, which only works for verified
      test recipients and inside the 24-hour window; outside it,
      `WHATSAPP_TEMPLATE_BACK_IN_STOCK` / `WHATSAPP_TEMPLATE_ABANDONED_CART`
      unset means that message queues a staff alert instead of going out
      automatically (`backend/services/notifications.py::send_proactive`)
- [ ] `DASHBOARD_SESSION_SECRET` set (a long random string) **and** at least
      one staff account exists (`python -m backend.cli create-staff`) — without
      the secret, `/dashboard` cannot log anyone in, and a conversation that
      pauses for a person has no way back to the customer

`GET /health` answers most of this:

```json
{
  "status": "ok",
  "llm_provider": "gemini",
  "llm_key_set": true,
  "whatsapp_configured": true,
  "shopify_configured": true,
  "shopify_webhooks_configured": true,
  "voice_notes": true,
  "image_understanding": true,
  "dashboard_configured": true
}
```

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
`_register_shopify_webhooks` (`backend/services/shopify_webhooks.py`) runs on
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
python -m backend.cli create-staff <username>   # prompts for a password twice
```

Everyone who can log in can do everything — one role, no admin/agent split —
so an account is who to hold responsible, not what to restrict. Deactivate
rather than delete someone who leaves (flip `Staff.is_active`, direct in the
database — there is no CLI for it yet) so their name stays attached to what
they already resolved.

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
| `LLM_MEDIA_MODEL` | blank | A separate model for voice and photos. Blank reuses `LLM_MODEL`. |

## Reading the logs

The logger names say where you are: `wanas.runtime`, `wanas.agent`,
`wanas.tools`, `wanas.dispatcher`, `wanas.media`, `wanas.channel.whatsapp`,
`wanas.webhooks.shopify`, `wanas.shopify`, `wanas.provider.gemini`,
`wanas.dashboard`.

Lines worth alerting on:

| Log line | What it means |
| --- | --- |
| `stripped a file path / tool call from a reply` | The prompt or a tool description has drifted. The sanitiser caught it, but this is the earliest signal. |
| `Refusing an order: Shopify unreachable` | Orders are being refused. Real revenue, right now. |
| `N Shopify variants have no SKU` | Run `scripts/shopify_set_skus.py --apply`; those variants cannot be sold. |
| `tool loop cap hit` | The model is looping instead of answering. |
| `rate limited or out of quota on model` | Gemini quota. Customers are seeing "الضغط عالي شوية". |
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
by design. Beyond that, look at the Gemini timeout and whether the Shopify
snapshot is timing out at eight seconds per turn.

**A conversation has gone silent.** It is probably paused on a handoff — check
`/dashboard` (see "The staff dashboard" above); it lists exactly these, oldest
wait first. Reply there, or resolve without a reply if it turns out to be a
false alarm. Only reach for the database directly
(`channel_identities.paused_until_staff_reply`) if the dashboard itself is the
thing not working.

## Database

Tables are created at startup from the models (`Base.metadata.create_all`).
That adds missing *tables*, never missing *columns* on an existing one — see
`scripts/migrate_add_shopify_order_columns.py` for what that costs when it
happens. Seed a fresh database with `python -m backend.cli seed`.

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
