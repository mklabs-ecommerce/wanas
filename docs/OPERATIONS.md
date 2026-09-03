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
      everything -- chat, voice-note transcription and photo reading -- but
      on two models: `LLM_MODEL` answers, `LLM_MEDIA_MODEL` reads the voice
      notes and photos, and leaving the latter blank falls back to a model
      that can hear rather than to the chat model, which cannot. Under
      `LLM_PROVIDER=gemini`, voice and photos both run on the Gemini key
      instead
- [ ] `SHOPIFY_STORE_DOMAIN` + `SHOPIFY_ADMIN_TOKEN` set, with the token's
      scopes covering **both** the chat path and the dashboard's Shopify
      section: `read_products`, `read_inventory`, `read_locations`,
      `read_orders` for the bot; `write_products`, `write_inventory`,
      `write_orders`, `write_fulfillments`, `read_customers` for the
      dashboard's product/order management, plus
      `read_merchant_managed_fulfillment_orders`,
      `read_assigned_fulfillment_orders` and their `write_` pair, which
      `read_orders`/`write_orders` do **not** imply and which the Ship button
      cannot work without -- plus the legacy-named `write_fulfillments`, which
      the *delivered* button needs on its own: `fulfillmentEventCreate` still
      asks for that one and the merchant-managed pair does not cover it -- the order drawer
      says so in place of the button, and `fulfill` refuses with
      `fulfillment_scope_missing`, rather than either one failing as an
      outage. A missing write scope shows up as a `store_unavailable`
      (config) refusal from the dashboard action that needed it, not a crash.
      `scripts/shopify_size_charts.py` uploads the size-chart diagrams to
      Shopify Files, which `write_products` already covers -- it says so
      plainly if a store's token does not. Deleting a file is the other half
      and needs `write_files`, which nothing here does automatically
- [ ] `read_publications` + `write_publications`, if products are to be
      created from the dashboard. **`status: ACTIVE` is not the same as being
      on the website.** A product Shopify creates is on no sales channel: no
      `publishedAt`, no storefront url, and it shows in no collection on the
      site. Without these two scopes the product is still created and still
      sells through the bot, and the create panel says in as many words that
      it is not on the website yet -- somebody then has to publish it by hand
      in Admin. With them, `shopify_publish_to_online_store` does it
- [ ] `SHOPIFY_VENDOR` set to the brand name (default `Wanas Gallery`).
      Shopify stamps a new product's vendor with the *store's* name unless
      told otherwise, which is not what the products already on the shelf say
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
      **Approval happens in Meta Business Manager, by hand — there is nothing
      in this repository that can do it.** Submit one template per name below,
      in Arabic, with no body variables (the client sends templates with none,
      so the approved copy has to stand on its own: "there's an update on your
      order, open the chat"), then put the approved names in the environment:

      | Env var | Message |
      | --- | --- |
      | `WHATSAPP_TEMPLATE_ORDER_UPDATE` | packed / shipped / delivered / cancelled |
      | `WHATSAPP_TEMPLATE_FEEDBACK_REQUEST` | the rating ask after delivery |
      | `WHATSAPP_TEMPLATE_ORDER_CONFIRMATION` | the confirmation, if the send is ever refused |
      | `WHATSAPP_TEMPLATE_BACK_IN_STOCK` | a waitlisted item is available again |
      | `WHATSAPP_TEMPLATE_ABANDONED_CART` | the idle-cart nudge |

      Until a name is set, the corresponding message can only reach a customer
      **inside** Meta's 24-hour customer service window (measured from their
      last inbound message). Outside it, nothing is sent: the line is written
      into the conversation marked as *not delivered* — the dashboard shows it
      dashed and red — and a `status_push_undelivered` /
      `proactive_outreach_failed` alert is raised for a person to phone the
      customer. That is the safety net, not the plan: order status is the one
      thing this shop cannot afford to communicate by accident, and a shipped
      parcel is routinely more than a day after the customer last wrote.
      (`domain/services/notifications.py`: `record_status_push`,
      `deliver_status_push`, `send_proactive`.)
- [ ] Staff replying from the dashboard are subject to the same rule: a
      conversation whose customer last wrote over 24 hours ago refuses the
      reply with `outside_window` and says to phone them, rather than sending
      something WhatsApp will drop
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

**What a comment can get.** Six outcomes, and only two of them ever write
anything a customer sees:

| The comment | What happens |
|---|---|
| A **fixed-answer question** — shipping cost, delivery time, payment | One public sentence from `assistant/comment_faq.py`, and that is the whole interaction: no DM, no session, no model call. The classifier is never asked. |
| `price` / `availability` / `size` / `variant` / `product_info` | A public line from that category's bank plus the DM handoff. They route identically and differ in *wording* — a size question opens on sizes, a price question on the price — which is the whole reason they are separate categories |
| `order_status` — someone waiting on an order | Its own voice (reassure, then redirect), the DM handoff asking for the order number, and an `order_status_comment` queue item. Keeps its public line even when the DM budget is spent |
| `complaint` — a customer with a real problem | A public line that admits nothing (nobody has checked the order yet) + the DM handoff + a high-priority `customer_complaint` alert. Keeps its public line when the DM budget is spent |
| `negative` — a hater, not a customer | One short, calm, un-defensive public line **and** a high-priority `negative_comment` alert. No DM: chasing a critic into their inbox is how a bad comment becomes a screenshot. This used to be silence, and silence was the bug — the line is written for the hundred people reading, not for the one who wrote it. It **acknowledges and stops**: it must not offer the DM, because no DM opens, and an invitation nobody honours is worse than no invitation. Whatever happens next happens because a person worked the alert, which is why it carries `complaint`'s priority |
| `positive` — a compliment | One short public thank-you. No DM, no like (Instagram has no API for liking a comment) |
| `tag_friend` — an @mention and nothing else | One light public line. The person worth answering is the friend about to open the notification. No DM, no alert |
| `spam` — follower bots, scam links, crypto | A `spam_comment` alert and nothing else — **the one category with no customer-visible answer**, because a public reply to a scam bot republishes it to everyone reading the post. The comment is still **not hidden**: hiding is invisible to the shop, so a misclassified customer would vanish with no trace. Hide by hand, from the alert |
| `other` — real text the model could not place | The polite catch-all: a public line that asks, plus the DM handoff. An unknown or retired category name lands here too, so a model on an old prompt degrades to a real answer rather than to silence |

**Every category answers somebody, and that is a tested property.** `_ACTIONS`
in `assistant/channels/instagram.py` is a table rather than an if/elif chain
precisely so `test_every_category_has_an_action_and_none_is_silent` can read
it: a missing branch used to be invisible, which is how `negative` shipped
classified, alerted on, and never answered.

**The words are banks, not lines.** `assistant/comment_replies.py` holds 4–8
hand-written variants per category, and the pick is
`crc32(comment_id) % len(bank)` — **deterministic, never random**, because
Meta redelivers any webhook it does not get a clean 200 for and a retry has to
reproduce the same sentence rather than post a second, differently worded
reply under one customer's comment. Every string is still fixed and
hand-written; nothing on the public surface is a sentence a model chose.

**Prices in public.** Meta does not prohibit stating a price in a comment
reply — the private-reply rules govern *initiating* a DM (one per comment,
inside 7 days), not what may be said publicly. The reason a product price
still goes to DM is accuracy, not policy: "بكام؟" under a post that shows
several pieces does not say which one, prices and sales change while a post
lives on, and a wrong number published under a post is worse than a question
asked. So the fixed, product-independent answers (shipping, delivery, payment)
*are* public, and anything needing a per-product lookup is answered in DM by
the agent, which reads live Shopify. The public handoff lines therefore never
claim the price has already been sent — that promise is tested against in
`test_no_public_line_ever_promises_a_price_it_does_not_send`.

The FAQ answers are a **lookup, not a model call** — three keys, three fixed
Arabic sentences, matched on a normalised comment. The 110 EGP in the
shipping answer is a hardcoded string because the rate is flat to every
governorate; the fee the bot quotes *in DM* comes from the `ShippingRate`
table, so **if the flat rate changes, both move** — `manage.py set-fee`
for the DM side, `assistant/comment_faq.py` for the public one. They have
their own rate limit, `INSTAGRAM_FAQ_RATE_LIMIT` (5/hour per commenter),
counted apart from `INSTAGRAM_COMMENT_RATE_LIMIT` (3/hour), because an FAQ
reply costs no DM and no model call.

**When the classifier is down.** A provider outage no longer produces public
replies. An unclassifiable comment is left alone and raises a
`classifier_unavailable` alert in the review queue carrying the text, the
comment id, the post and the commenter — enough to answer it by hand.
A run of those in the queue means the LLM provider, not the comment surface:
check the key, the quota, and `COMMENT_CLASSIFIER_MODEL` if one is pinned.
Silence is the intended failure here; the alerts are what stop it meaning
lost customers.

**Comments never arrive at all — check the subscription first.** Turning
`INSTAGRAM_COMMENTS_ENABLED=1` on does nothing if Meta is not sending comment
events, and the two failures look identical from here: silence. Ask Meta what
it is actually subscribed to before touching anything in this repo:

```bash
curl -s "https://graph.instagram.com/v23.0/me/subscribed_apps?access_token=$INSTAGRAM_ACCESS_TOKEN"
```

`subscribed_fields` must contain `comments`. A field list of just
`["messages"]` is the DM half working perfectly and the comment half never
having been switched on at Meta's end — no webhook call is made, so no log
line in this app records the comment, and nothing in the codebase can be at
fault. Subscribe with:

```bash
curl -s -X POST "https://graph.instagram.com/v23.0/me/subscribed_apps"   -d "subscribed_fields=messages,messaging_postbacks,messaging_seen,comments"   -d "access_token=$INSTAGRAM_ACCESS_TOKEN"
```

Still **not** `message_echoes`. Subscribing is separate from, and additional
to, the field list ticked in the Meta app dashboard; the call above is what
the account actually honours.

**One account, two ids.** `GET /me?fields=id,user_id` returns both: `user_id`
is `INSTAGRAM_ACCOUNT_ID` (what addresses the Graph endpoints) and `id` is
`INSTAGRAM_APP_SCOPED_ID`. Set both. Which one Meta stamps on the shop's own
comment or an echo is Meta's choice, and the "never answer yourself" check
tests membership of the pair — with only one set, the shop's own comment can
arrive wearing the other number and read as a stranger's.

**The two rate limits, and which one stops what.**
`INSTAGRAM_FAQ_RATE_LIMIT` (5/hour per commenter) is the **flood guard**: it
caps how many comments from one person are examined at all, and so caps the
model calls a spammer can cost. Tripping it drops the comment and raises one
`comment_flood` alert. `INSTAGRAM_COMMENT_RATE_LIMIT` (3/hour) is the **DM
budget**, and it is spent where the DM is actually sent — not at ingest.

That split is load-bearing. Charging the DM budget at ingest meant a comment
that only ever gets a public line spent it: three compliments under a post
used up all three DMs, and the real question that followed them was dropped
with no reply and no public line either. A compliment and an FAQ answer cost
nobody an inbox, so neither may spend the budget that protects one. When the
DM budget *is* gone, the public "check your DMs" line is withheld with it —
promising a DM that is not coming is worse than saying nothing — except on a
`complaint`, which keeps its public line because silence on a public
complaint is the worst outcome available here.

**Turning comments off in a hurry:** set `INSTAGRAM_COMMENTS_ENABLED=0` and
redeploy. The DM half keeps working; only the public surface goes dark.
`INSTAGRAM_PUBLIC_REPLY_ENABLED=0` is the softer version — the bot stops
speaking in public but still answers in DM, including the questions it would
have answered publicly. Comments also ship with per-commenter rate limits and
drop the shop's own comments before anything else runs — but if the bot is
ever seen replying to itself under a post, `INSTAGRAM_COMMENTS_ENABLED=0` is
the kill switch.

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

## Emailing the owner

The dashboard queue is the record of everything staff have to act on, but it
only helps someone who is already looking at it. Three kinds of queue item
cannot wait for that, so they are also emailed to the owner:

- a **negative or complaining Instagram comment** (`negative_comment`,
  `customer_complaint`, `comment_flood`) -- public, and getting worse the
  longer it sits;
- the **bot failing** -- `turn_crashed`, `reply_delivery_failed`,
  `instagram_reply_delivery_failed`, `classifier_unavailable`,
  `instagram_token_refresh_failed`, and the three "composed it but could not
  deliver it" reasons (`confirmation_delivery_failed`,
  `status_push_undelivered`, `proactive_outreach_failed`);
- **every `request_human` handoff**, whatever its reason. A handoff *pauses*
  the conversation: the bot will not answer that customer again until a person
  replies or resolves it, so an unread handoff is a customer sitting in
  silence;
- **an order changing after it was placed** -- `order_modified`,
  `order_cancelled`, and every `item_swap` request. Each one is stock or money
  moving, and a swap is a customer waiting on a decision only a person can
  make: the order does not move until somebody makes it;
- **stock running out** (`low_stock`), the one alert that is cheaper to act on
  early than late.

One reason is deliberately left out, and it is the loud one:
**`order_confirmed`**. It fires on every successful sale -- the outcome the
whole system exists to produce. An address that carries it is an address that
gets filtered, and filtering it would cost every alert above. The list lives
in `domain/services/alert_email.py`; moving that line is a decision about the
owner's attention, not a formatting change.

### Railway blocks SMTP. This is why there are three transports.

Every outbound SMTP port is blocked from inside the container. Tested there:

```
smtp.gmail.com:587   FAIL after 25.0s  Network is unreachable
smtp.gmail.com:465   FAIL after 25.0s  Network is unreachable
:25, :2525           FAIL              Network is unreachable
example.com:80       OK in 0.0s
```

General egress is fine -- only SMTP is blocked, as a platform policy against
spam. So a Gmail **App Password** works from a laptop and cannot deliver a
single alert from the deploy, however correct it is. That is not a
misconfiguration to hunt; it is the network.

Two transports send over HTTPS (443) instead, and either delivers from the
deploy. `send_email` picks in order: **Resend** when `RESEND_API_KEY` is set,
else the **Gmail API** when its three values are, else **SMTP**. `GET /health`
reports which, as `alert_email_transport`, and the boot log says so too -- a
deploy logging `over SMTP` is a deploy whose alerts will never arrive.

Resend goes first because it is the only one of the three with nothing in it
that expires. The Gmail route's cost is its refresh token: Google kills it
after seven days while the consent screen is in Testing, and on a password
change or a revoked grant. That failure is the worst kind here, because the
thing that broke *is* the warning channel. Gmail stays as the route that needs
no third-party vendor and sends as the shop's own mailbox; SMTP stays for local
development and for any host that permits it.

### Setting up Resend (production)

Two variables, one of them optional:

| Variable | Notes |
| --- | --- |
| `RESEND_API_KEY` | From the Resend dashboard. A credential -- Railway and `.env` only, never committed or logged. |
| `RESEND_FROM` | A sender on a domain verified in Resend. Blank falls back to `onboarding@resend.dev`. |

The fallback sender needs no DNS work but delivers **only to the Resend
account owner's own address**, which is who these alerts go to anyway -- so it
works unconfigured. Verifying a domain and setting `RESEND_FROM` to something
like `alerts@wanasgallery.com` is what stops the owner's inbox filing the
shop's alerts as a stranger's. A send refused for an unverified sender is
logged as `resend refused the alert email ... domain is not verified` and
returns False; the alert is still in the staff queue.

### Setting up the Gmail API (production)

Run `python scripts/gmail_authorise.py` **on your own machine**; the Google
Cloud steps are written out at the top of that file. It opens a browser once
and prints a refresh token.

One of those steps matters more than it looks: **publish the OAuth consent
screen**. While it is left in *Testing*, Google expires every refresh token
after **seven days**, so the alerts would stop a week after they started with
nothing to show for it. Published-but-unverified is fine for a single user --
you will see an "unverified app" warning and can continue past it.

| Variable | Notes |
| --- | --- |
| `ALERT_EMAIL_TO` | Where the alerts go. Blank = feature off. |
| `GMAIL_CLIENT_ID` | From the Desktop-app OAuth client. |
| `GMAIL_CLIENT_SECRET` | The same. A credential -- never logged or committed. |
| `GMAIL_REFRESH_TOKEN` | From the script above. Also a credential. |
| `ALERT_EMAIL_FROM` | Cosmetic: Gmail rewrites it to the authenticated mailbox. |
| `ALERT_EMAIL_COOLDOWN_SECONDS` | `900`. Same reason + same customer stays quiet this long after one mail. |
| `ALERT_EMAIL_MAX_PER_HOUR` | `20`. Hard ceiling across everything. |

**If the alerts stop, look here first.** The refresh token is the fragile
part of this route: it dies on a Google password change, on a revoked grant,
and after seven days if the consent screen was never published. The failure
is loud in the log (`Gmail refused the refresh token ... invalid_grant`) and
silent everywhere else, because the channel that would have warned you *is*
the one that broke. Mint a new token with the same script.

### The SMTP half (local, or a host that allows it)

| Variable | Example | Notes |
| --- | --- | --- |
| `ALERT_SMTP_HOST` | `smtp.gmail.com` | Default. |
| `ALERT_SMTP_PORT` | `587` | STARTTLS. `465` for implicit TLS. |
| `ALERT_SMTP_USERNAME` | `shop@gmail.com` | The sending mailbox. |
| `ALERT_SMTP_PASSWORD` | *(app password)* | 16 characters; Gmail refuses an account password. |

With neither transport configured nothing is sent and every alert still
reaches the dashboard queue exactly as before -- the same "not configured is a
documented off state" shape as Shopify and Instagram.

The two limits exist because a crash loop or a comment flood raises one queue
item **per event** by design. One email per event would be the log file in the
owner's inbox, and a suppressed mail is only ever a duplicate of one already
sent -- the queue item is always written either way.

The cooldown keys on *what the alert is about*, not on the customer: the
order and stock reasons carry no `external_id` at all (they are raised about
an order or a variant, not about whoever happened to be typing), so it falls
back to the order id and then the variant id. Two different products running
low inside the same window are two emails; the same product twice is one.

Sending happens on a daemon thread, hung off the transaction's commit. That is
deliberate on both counts: an email about an order that later rolled back is a
person opening the dashboard to look for something that does not exist, and a
two-second SMTP round trip on the order path is a two-second stall for the
customer.

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

**A conversation has gone silent.** It is probably paused — either on a
handoff or because a staff member took it over — and a paused conversation
stays paused until someone presses **رجّع البوت** (`/release`). Sending a
reply does *not* hand it back; that is deliberate, so the bot cannot answer
over a person mid-conversation. Check `/dashboard` (see "The staff dashboard"
above); it lists exactly these, oldest wait first. Reply there, hand it back
when the exchange is finished, or resolve without a reply if it turns out to be
a false alarm.

If nobody is available to work the dashboard, `python manage.py release-conversation
<external_id> [--channel]` clears the flag and resolves the open handoff items.
Only reach for the database directly
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
python scripts/shopify_reconcile_products.py   # drop wanas.db products Shopify no longer has
python scripts/shopify_size_charts.py          # publish the size charts to Shopify
python scripts/shopify_size_charts_import.py   # and read edited ones back
```

The two size-chart scripts are a pair, and which one to run depends on where
the chart was authored. `shopify_size_charts.py` pushes `data/size_charts.json`
out so the storefront can render it. `shopify_size_charts_import.py` brings
back what has been edited or added in Shopify Admin since, so the bot quotes
the numbers the product page shows instead of the ones the file shipped with.
The import never deletes: a product whose metafields are empty is left alone,
and a chart the database already agrees with is skipped rather than replaced
by a CDN url. A chart authored in Admin with no `chart_id` of ours lands under
`shopify-<product_id>`, so an edit made on one t-shirt cannot rewrite the
chart its two siblings share.

### A product added in Shopify Admin

It reaches the bot on its own, by two routes.

The fast one is the webhook: `products/create` and `products/update` are
registered alongside the order topics and run `product_import` for that one
product, so the product is searchable seconds later. **This route needs
`SHOPIFY_WEBHOOK_SECRET`** -- without it the endpoint refuses every delivery
with a 503, which is correct (an unauthenticated way to write to the catalogue
is worse than no integration) but means the webhook does nothing.

The floor under it is the scheduler: every tick
(`REENGAGEMENT_INTERVAL_SECONDS`, 30 minutes by default) imports whatever is
new, so a product added in Shopify Admin reaches customers within the half
hour even on a store where the secret was never set. It costs one list read
when nothing has changed -- a product whose SKUs are already known never costs
a detail call -- so it is cheap enough to leave on. The boot-time reconcile is
the third pass, for whatever both of those missed.

Two things it deliberately will not do. It skips a product still wearing only
Shopify's own "Default Title" placeholder -- that is a half-made product, and
mirroring it writes a phantom row at 0.00 the bot offers and cannot sell;
`products/update` is subscribed precisely because Shopify fires `create` at
that moment, and the real sizes arrive a beat later. And it refuses a variant
carrying a SKU that is not ours rather than re-keying it. The one exception is
recognition rather than a guess: a SKU that rebuilds itself exactly from the
`product_id-size-colour` convention was written by this codebase, so a
dashboard create that pushed to Shopify and died before mirroring is adopted
under the id its own SKUs already carry, with nothing written back to Shopify.

If a product is invisible to the bot, `import_missing_products(db, apply=False)`
says why in one line -- either it is adopted, or the SKU on it belongs to
somebody else's scheme and wants a human.

`shopify_reconcile_products.py` is the one that deletes, so read its dry-run
report before `--apply`. A product that has ever been ordered is **archived**
rather than deleted -- the order lines are the record that money changed
hands, and they still read. It refuses to run at all if Shopify returns no
SKUs (an outage, or a token pointed at the wrong store), and refuses if more
than half the catalog looks gone; `--force` lifts only the second of those,
and you should check `SHOPIFY_STORE_DOMAIN` and the token before reaching for
it.

## Product photos

`data/images/` is what this sample shipped with — a real deployment does not
need to keep bundling photos in the repo. Attach a photo to a product or
variant in Shopify Admin the normal way; the next message that reads that
variant already picks it up (`shopify_catalog.fetch_all` reads it alongside
price and stock, same call, no extra step and nothing to run). It only takes
over from the local file for the colour it was set on — a product with no
photo on Shopify yet keeps serving `data/images/` exactly as before. See
`docs/ARCHITECTURE.md` ("Shopify owns price, stock and orders").
