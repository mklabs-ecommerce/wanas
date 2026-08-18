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
- [ ] `SHOPIFY_STORE_DOMAIN` + `SHOPIFY_ADMIN_TOKEN` set
- [ ] `SHOPIFY_WEBHOOK_SECRET` set **and** the webhooks registered (below) —
      without it orders stay `Confirmed` forever and no tracking message ever
      goes out
- [ ] Meta webhook registered and verified
- [ ] A real shipping fee set for every governorate you deliver to
      (`python -m backend.cli set-fee`) — an order for an unpriced governorate
      is refused, on purpose
- [ ] SKUs linked (`python scripts/shopify_set_skus.py --apply`), then
      `python scripts/shopify_check_live.py` reports no disagreement
- [ ] Meta templates approved for the proactive messages (order confirmation,
      status pushes, feedback request). Until then they go out as free-form
      text, which only works for verified test recipients and inside the
      24-hour window.

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
  "image_understanding": true
}
```

## Registering the webhooks

**Meta → this app.** Callback URL `https://<host>/webhooks/whatsapp`, verify
token = `WHATSAPP_VERIFY_TOKEN`. Subscribe to the `messages` field. The
handshake is a `GET`; the app answers it only when the token matches.

**Shopify → this app.** Notification URL `https://<host>/webhooks/shopify`,
format JSON, and subscribe these topics:

| Topic | What it does here |
| --- | --- |
| `orders/fulfilled` | Order → `Shipped`, customer gets "طلبك في الطريق ليك 🚚" |
| `orders/partially_fulfilled` | Order → `Packed` |
| `fulfillments/update` | Order → `Delivered` when Shopify says `delivered` |
| `orders/cancelled` | Order → `Cancelled`, stock returned locally |

Every delivery is HMAC-verified against `SHOPIFY_WEBHOOK_SECRET`; with no
secret configured the endpoint refuses everything, because an unauthenticated
way to cancel orders is worse than no integration at all.

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
`wanas.webhooks.shopify`, `wanas.shopify`, `wanas.provider.gemini`.

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

**A conversation has gone silent.** It is probably paused on a handoff. Until
there is a staff UI (see ARCHITECTURE.md, "Known gap") the only way back is a
staff action against `channel_identities.paused_until_staff_reply`.

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
