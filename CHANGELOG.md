# Changelog

## Unreleased — Everyone who ever bought, in one list that filters

Four things, all the same complaint from different angles: the Customers
screen was not showing everyone, and the one filter that would have narrowed
it was broken.

- **The order-count filter returned nothing, always.** Shopify sends
  `numberOfOrders` as a *string* — `"1"` — and `admin_customers._summary`
  passed it straight through, so "customers with exactly one order" compared
  `"1"` to `1` and matched none of the customers with exactly one order.
  `admin_orders._customer_orders` had coerced it correctly since it was
  written; this is the same reading in the place the customers list goes
  through. The reason no test caught it: the fake shelf replaces
  `list_customers` wholesale, so the mapper never ran in the suite. It does
  now, over a real Shopify payload.
- **"كل المتجر" now means the whole store.** It was Shopify's customer list
  alone, which was short by exactly the buyers whose orders the bot placed
  before it attached a customer to them — they exist in wanas.db with a name
  and a governorate, and were missing from the list that called itself
  everyone. Merged on the phone, normalised through the order path's own
  `normalise_phone`, so `01067177128` and `+201067177128` are one person. A
  local row that matches a Shopify customer is dropped rather than summed:
  `numberOfOrders` is already that person's lifetime total across both
  channels. Each row says which side it came from, and the store list now
  always pages the whole customer list, because deduping against page one
  would list anyone on page two twice.
- **Both tabs offer the same four filters.** Order count, governorate and sort
  were on the store tab only, on the reasoning that they were Shopify-side
  facts — they are not: a bot customer's governorate is on their `Client` row
  and their order count is in `orders`. `dashboard/customer_filters.py` holds
  the one vocabulary both routers use, so the filter bar cannot change shape
  when you switch tab. The counts still mean different things and the label
  says so: lifetime orders on the store tab, bot orders on the bot tab.
- **Search runs while you type**, debounced 300ms rather than per keystroke —
  on the store tab each query pages the whole customer list. Enter still works
  and skips the wait. The re-render that follows each result rebuilds the
  toolbar, so focus and caret are put back on the fresh input; without that,
  search-as-you-type would eat the field on every result that came back.

Alongside them, the repair for the orders already placed:

- **`scripts/shopify_backfill_customers.py`** attaches a customer to the
  orders that have none. I said earlier this was impossible — that was wrong:
  `orderCreate` is the only place a customer can be *upserted*, but
  `orderCustomerSet` links an order to a customer that already exists, so the
  repair is find-or-create then link. Dry run by default like every other
  script in `scripts/`, and the dry run is the supervision: one line per order
  naming who it would link. An order with no phone and no email is skipped —
  a name is not an identity, and linking two people who share one is worse
  than the "No customer" it replaces. Idempotent, and two orders from one
  person converge on one record.

## Unreleased — The order says who placed it

Every sale the bot made reached the Shopify admin with a shipping address and
no customer, which the Orders list renders as **No customer** — 23 of the last
28 orders in the shop. The name was never missing; it was only on the address,
where nothing but the packing slip reads it.

- **`orderCreate` now attaches the customer** (`shopify_orders._customer`),
  using `toUpsert`: Shopify matches on the phone and links the record the
  customer already has instead of making a second one. With neither a usable
  phone nor an email there is nothing to match on and nothing Shopify would
  create a customer from, so the block is omitted rather than sent in a form
  that could cost the sale.
- **A refusal about the customer costs the link, not the sale.** Their phone
  sitting on somebody else's record retries once without the block and logs
  why. An out-of-stock refusal is never retried — the second call is refused
  for the same reason, and the customer is owed the alternatives it raises for.
- **The name is split once**, by `_split_name`, for both the customer record
  and the address, so the Customers list and the parcel cannot disagree about
  the same person.
- **The dashboard reads the name off the address when there is no customer
  record**, which is what the existing orders get: `orderUpdate` has no
  customer field, so nothing can attach one to an order already placed —
  verified against the 2025-01 schema, not assumed. The fallback is the name
  only. `customer_order_count` stays `None` and the returning/new chip stays
  **unknown**, because an address cannot say whether this person has bought
  here before, and "new" is not a thing to guess at.

## Unreleased — Who sees the dashboard, and what the numbers are actually of

The dashboard has had one role since it existed: everyone who could log in
could do everything, and attribution was the only control. That was true while
the only two logins belonged to the people who built the shop. It is not a
staffing model. Alongside it, four screens answered questions slightly to the
side of the ones being asked — "how many orders" with no way to separate the
cash-on-delivery ones, "the customers" with no way to ask which of them come
back, a channel breakdown that called every Instagram sale a WhatsApp one.

- **Staff accounts now carry a role and a permission list**, and the owner
  manages both from a new **الفريق** section under القرار. One permission per
  dashboard section. `manage.py create-staff` grew `--role` / `--can`, and
  still defaults to `owner` so the first account on a fresh database can reach
  the screen that scopes everyone else.
- **Enforcement is on the endpoints, not in the sidebar**
  (`dashboard/guard.py::require_permission`). Hiding a nav item is a courtesy
  to whoever is reading it; the route behind a hidden button is one `fetch`
  away, so each one refuses on its own with a 403. The first test written for
  this asserts exactly that.
- **A `Staff` row with no role stored reads as an owner.** Both new columns
  are nullable, which is also what lets `domain/schema_drift.py` add them at
  boot — it reports a `NOT NULL` column with no server default rather than
  guessing at one. Reading an absent role as "staff with no permissions"
  would have shipped a deploy in which every existing login was scoped to
  nothing and nobody could open the screen that fixes it.
- **Orders and Analytics split cash-on-delivery from online**, classified once
  in `admin_orders._payment_method` from the order's `paymentGatewayNames` so
  the list and the statistics page can never disagree about what COD counted.
  An order with no gateway is `unknown`, never `online` — a draft is not
  evidence that somebody paid a card, and folding it in would inflate the
  exact number the toggle exists to separate.
- **The channel a sale came in on is read from `Order.source_channel`**, not
  from the Shopify tags. `ORDER_TAGS` hardcodes `whatsapp` on every order the
  bot places, Instagram included, and no tag can be added retroactively to the
  orders already in the shop. Analytics gets a Web / WhatsApp / Instagram /
  All toggle on the same footing as the payment one — both narrow every number
  on the page, not one chart. The money is still summed from Shopify; only the
  label comes from Postgres.
- **Orders can be filtered to returning or new customers**, from Shopify's own
  lifetime `numberOfOrders`. An order with no customer record is a third
  bucket, never counted as new.
- **Customers filters by order count (1, 2, 3, or any number typed in) and by
  governorate, and sorts by order count.** None of those three is expressible
  in Shopify's customer search or its `CustomerSortKeys`, so the moment one is
  set the route pages through the whole matching list rather than filtering
  the first fifty rows — "the customer with the most orders", computed over
  page one, is the most of that page, and nothing on screen would have said
  so. The page cap is surfaced as `truncated`. The governorate dropdown comes
  from the shop's own `ShippingRate` list, so it offers a governorate the shop
  ships to but has not sold to yet.
- **Two new things to look at**: orders per governorate, and a
  conversation-to-order rate. The second is deliberately not called "معدل
  التحويل" — the Admin API this app reads exposes no site traffic at all, so a
  real conversion rate is not derivable. It is bot orders over bot
  conversations, computed in the browser from the two payloads the page
  already loads, with a caption on screen saying so. The denominator moves
  with the channel toggle (`insights.by_channel`); under the payment toggle,
  and on the website, there is no denominator that matches, so the card is
  hidden rather than dividing by something that does not.
- **Both the Orders and the Customers list page through the whole match once
  a filter is on.** Filtering the first fifty rows would answer a different
  question with a straight face — "the COD orders" would mean "the COD ones
  among the last fifty orders", and the totals above the table would sum that
  subset while reading like store figures. The page cap surfaces as
  `truncated` and the UI says the numbers are a floor.

- **Analytics takes a specific window, not just 7/30/90.** `dashboard/ranges.py`
  parses one date window for both tabs, because two tabs of one page answering
  about different fortnights is the failure that module exists to prevent. A
  single day is `start == end`. `insights_api` needed the real work: its
  zero-filled series anchored on *today* and every filter was `>= since` with
  no upper bound, so a historical range would have drawn a flat line of zeros
  across dates that had real activity, and swept everything after the window
  into the totals. Both are fixed and both are tested. Bad input is refused
  (400), never repaired: `start` after `end` is not silently swapped.
- **A language toggle, Arabic ⇄ English.** Arabic stays the source language and
  the dictionary is keyed on it, so a missing translation falls back to Arabic
  rather than to a blank. Because that makes an omission invisible,
  `tests/test_dashboard_i18n.py` extracts every phrase the page asks for and
  fails on any that is unaccounted for — all 527 are.
  - Templates are translated through a **tagged** template, `TR`, which is what
    makes this safe: a tagged template receives its literal chunks separately
    from its `${...}` values. The chunks are developer copy; the values are
    where a customer's name arrives. No customer data can reach the dictionary.
  - `QUICK_REPLIES` is excluded by name. It is typed into the composer and sent
    to a customer over WhatsApp — the UI language is the staff member's
    preference, and an Egyptian customer should not get an English message
    because somebody's dashboard was in English.
  - `dir` flips in a head script, before first paint. That one attribute is the
    whole RTL/LTR switch, because the stylesheet was already written in logical
    properties throughout — a test now keeps it that way.
  - Strings that live on the server (the feature flags, the permission
    catalog) carry both languages in the payload, since the page's dictionary
    has never seen them.
- **The brand mark is the real logo, and the name is always "Wanas Gallery".**
  It is a name, not a label, so it is excluded from the dictionary and reads
  the same in both languages. The file is served from `/dashboard/logo.webp`
  rather than inlined, so the shop can swap it without editing HTML.

## Unreleased — The governorate in the address the customer already typed

A customer wrote "شبين الكوم المنوفية شارع 9" and the bot answered with the
region picker, as though they had not said where they live. Everything needed
to read it was already there — `shipping.resolve` knows the twenty-seven names,
their Arabic labels, and the districts people use instead of them. What was
missing was anyone looking: `ask_governorate` offered a list without reading
the message it was answering.

- **`shipping.detect` reads the governorate out of free text**, and
  `ask_governorate` calls it before sending anything. One match and the tool
  returns `step: "done"` with the key, so the turn goes straight to
  `get_shipping_fee`. No match and the region picker is sent exactly as
  before — free text is still never a source of new values, only a way of
  selecting one of the fixed twenty-seven.
- **Matching is by whole word, which is the whole defence against a false
  positive.** The substring test underneath the old last-resort match found
  "قنا" inside "القناة", so an address on شارع القناة in Ismailia resolved to
  Qena — 700km and a different fee, on a path `confirm_order` also runs
  through. Tokens are compared instead, with the definite article stripped
  from both sides, and the longest name at a position wins ("مرسى مطروح" over
  "مطروح").
- **Two governorates in one message is not a decision the bot makes.** It
  returns `step: "confirm"` with those two, and the picker it sends offers
  those two rather than all twenty-seven.
- Only the customer's own last two messages are scanned. A governorate the
  *bot* named is not the customer stating where they live, and one mentioned
  six messages ago is not this order's address.

## Earlier unreleased — The bot remembers, and knows what it is being asked about

Two complaints that read as one ("the bot gets confused") and share nothing
underneath.

- **It forgot the product.** `HISTORY_CAP` answered both "what is stored" and
  "what does the model see", and it counts messages, not exchanges. One
  customer question costs four to six — the question, an assistant message
  carrying tool calls, the `tool_results`, then the reply — so forty messages
  was about seven exchanges, and a product discussed at the start of a
  conversation was gone by the time the customer asked a follow-up about it.
  `assistant/context.py` splits the two: the last `MODEL_CONTEXT_MESSAGES`
  (24) go through verbatim, and up to `MODEL_CONTEXT_RECALL` (60) older ones
  are compacted to the sentences that were actually said, with the catalog
  dumps underneath them dropped. `HISTORY_CAP` itself goes to 150. Nothing is
  summarised: compaction only removes whole messages, so every sentence the
  model reads is the exact sentence that was said, and the stored transcript
  keeps every tool exchange for the dashboard.
- **It answered the wrong message.** A WhatsApp "reply to this" arrives as
  `context.id`, and the only thing it could ever be matched against was the
  other messages in the same debounce batch — so a reply to something the bot
  said was unresolvable outright (nothing recorded the id WhatsApp gave a
  message the shop sent; the send's response body was read for a status code
  and thrown away), and a reply to an earlier message of the customer's own
  fell outside the batch. Either way the quote was dropped and the model
  guessed from recency. Now every stored message carries the platform ids it
  was sent or received as (`mids`), outbound ones stamped on after delivery
  by `session.attach_outbound_ids`, and `assistant/quoting.py` resolves a
  quoted id against the whole transcript — archive included — folding the
  original sentence into the turn. An id it cannot find is left unannotated
  rather than guessed at. Both channels: WhatsApp and Instagram DMs.
- `Pending` also labels text fragments now, not only photos and voice notes,
  and hands on only the quotes it cannot explain itself
  (`unresolved_reply_to`), so no quote is ever described twice.

## Earlier unreleased — Every message the shop sends, in the transcript

Two bugs found by comparing a customer's WhatsApp thread against the same
conversation in the dashboard. The dashboard was showing strictly less than
had actually been said, and one of the missing messages should never have
been sent at all.

- **Nothing recorded a message the shop started.** An agent turn writes its
  reply to `sessions`; an order confirmation, a Shopify status push, the
  delivery feedback request, a back-in-stock notice and an abandoned-cart
  nudge do not go through a turn, so they went to the sender and nowhere
  else. `domain/services/notifications.py` now writes each of them through a
  registered port (`register_transcript_recorder` →
  `assistant/runtime.py::record_outbound`), inside the transaction that
  decides the message so it commits or rolls back with it. Stored as
  `by="system"`: a third voice next to the model and a staff member, rendered
  distinctly in the conversation view, and skipped by the inbox's
  `unanswered` filter — a nudge sent on a clock is not somebody answering
  the customer.
- **"Back in stock" for an item that had never been away.** `add_to_cart`
  was the one place in the bot still reading `variants.stock_qty`, the
  seeded wanas.db column, instead of the live Shopify overlay every price
  and status it quotes already comes from. `cairokee-hoodie-s-black` reads 0
  there and 1 on Shopify, so the bot refused a size the storefront was
  selling — and because a refusal is what joins the stock waitlist, the
  scheduler read Shopify half an hour later, saw a positive number, and
  announced a restock that had never happened. It reads the overlay now.
- **And the notice itself now has to prove the change.** `StockWaitlistEntry`
  gained `observed_stock`: what Shopify said the level *was* when the
  customer was turned away. `check_back_in_stock` sends only when that
  baseline is at or below zero and the level is above it now — an actual,
  verified transition rather than "the number looks healthy today". An entry
  is only created against a level Shopify confirmed, so a Shopify outage no
  longer manufactures one; rows written before the column existed are
  baselined on the first pass instead of fired on. Added additively by the
  startup schema reconciliation, no migration to run by hand.

## Unreleased — The photo of the colour that was asked for

A customer who asked for the olive hoodie was reliably shown the black one,
and the Inventory table put the same photo next to all four colourways of the
RINGER TEE. Three separate causes, all in the path between Shopify's photos
and the person looking at one.

- **The bot never knew which colour to show.** `get_variants` took a
  `product_id` and nothing else, and `_candidate_images` walked
  `color_images` in dict order — so every request got whichever colourway
  came first. The tool now takes an optional `color`, and the runtime sends
  that colourway's photo. Switching colour mid-conversation is a new
  question too: the "already shown this product" guard is now scoped to the
  colour, where it used to swallow the reply that should have carried the
  new photo.
- **Shopify's answer was only ever allowed to lead a guess.** The seeded
  `data/images/` mapping was built by matching folder names, and three of
  the RINGER TEE's four colours point at a different product's folder
  entirely — "another angle of the navy one" answered with a shirt this shop
  no longer sells. Once Shopify has a photo for *every* colourway,
  `_overlay_images` now takes it as the product's photo set rather than
  putting it in front of the seed. Short of full coverage the old additive
  rule is untouched, so nothing loses a photo it had. Five products that
  `wanas.db` never split by colour get their split from Shopify for the
  first time.
- **A colourway's lead photo is a majority vote** across its variants. HEART
  TOP's large olive has the black photo attached to it in Shopify Admin, and
  taking whichever variant sorted first meant one staff mis-click decided
  what "the olive one" looked like.
- **The dashboard was reading the product's featured image for every
  variant row.** Inventory rows and the product drawer's variant table now
  show the variant's own photo, which is also what makes a mis-attached one
  like HEART TOP's visible to staff instead of only to a customer.

All 52 product/colour combinations in the live store now resolve to their own
photo. The Shopify-unreachable fallback, the size-chart path, the one-photo
budget and `more_images` are unchanged.

## 1.2.0 — Full re-architecture: layered by responsibility, vendor-cohesive integrations

Pure structural move, no behavior change. `backend/` and `chatbot/` are
gone, replaced by eight top-level packages named for what they actually do:

- `common/` — the zero-dependency shared kernel (money, events, the shared
  Meta webhook-signature check, the file-serving path guard).
- `config/` — `settings.py`, imported by every layer below.
- `domain/` — persistence and business rules (`db.py`, `models.py`,
  `legal.py`, `seed/`, `services/`); must never import `assistant/`.
- `integrations/` — every outbound vendor client, one package per vendor
  (`shopify/`, `whatsapp/`, `instagram/`) instead of the same six Shopify
  files scattered across three old directories.
- `assistant/` — the AI agent runtime (was `chatbot/`): agent loop,
  dispatcher, session store, providers, tools, channel adapters, harness.
- `api/` — `public_media.py`, the one FastAPI route with no home in any
  other layer.
- `dashboard/` — unchanged, already a clean, self-contained package.
- `manage.py` — the ops CLI, promoted from `backend/cli.py` to a root
  entrypoint (`python manage.py <cmd>`), matching `app.py`'s own
  `uvicorn app:app` pattern.

One real bug fixed along the way: `conversation_reset.py` imported the
assistant layer directly, breaking the project's own "domain never imports
assistant" rule. Replaced with a registration callback (the same shape
`notifications.py` already used for outbound senders) instead of just
relocating the violation under a new name.

Full test suite and `ruff check .` verified clean after every step; see
`OPENCODE_PROGRESS.md` for the complete file-by-file move log.

## 1.1.0 — Instagram, a second first-class channel

Instagram DMs (`"instagram_dm"`) run on the same agent, tools, carts, orders
and staff dashboard as WhatsApp. Everything channel-specific lives in two
files — the inbound adapter (`chatbot/channels/instagram.py`) and the
outbound client (`backend/integrations/instagram_client.py`) — plugged into
machinery that was already channel-neutral.

- **Per-channel sender registry.** The Notification service's single module
  global became a registry keyed by channel; an unregistered channel falls
  back to logging and *never* to another channel's client. This is what makes
  "a staff reply to an Instagram conversation posted over WhatsApp, to an
  IGSID used as a phone number" structurally impossible rather than a bug to
  avoid. Orders now remember `(source_channel, source_external_id)` so their
  confirmations and status pushes go down the thread they were placed in.
- **Public media route.** Instagram cannot receive an upload; Meta fetches
  URLs itself. `api/public_media.py` serves catalog files (the size
  charts) behind a deterministic HMAC path token — 404 on anything wrong,
  `data/inbound` unreachable even under a correct token for another path.
- **Platform limits enforced below the model:** replies split at ~950 bytes
  of UTF-8 (Arabic is two bytes per character); quick replies capped at 13
  with 20-character titles; the 27-governorate picker degrades to a numbered
  plain-text list that lands on `shipping.resolve`'s free-text handling; no
  templates, so proactive outreach outside a live conversation becomes a
  staff alert by design.
- **Comments, shipped OFF** (`INSTAGRAM_COMMENTS_ENABLED=0`). A strict filter
  chain drops the shop's own comments first of all, then threaded replies,
  duplicates, old comments, floods and emoji-only noise; what survives gets
  one fixed public ack plus one private reply that seeds the DM session.
  One private reply per comment, ever — recorded before it is sent.
- **The 60-day token.** Instagram tokens expire silently after 60 days;
  `integrations/instagram/token.py` refreshes them from the scheduler
  within ten days of expiry, stores the result where the client reads it,
  alerts staff when it fails, and reports the expiry on `/health`.
- Dashboard: channel badges (WA/IG), a channel filter, and a visible warning
  when replying to an Instagram conversation whose 24-hour window may have
  closed. Prompt: surface-aware — WhatsApp's system prompt byte-identical,
  Instagram gets its own lines. Docs: `docs/OPERATIONS.md` has the launch
  checklist and the kill switch.

## 1.0.0

The release that made the bot safe to point at real customers. Five things
were either silently broken or silently missing; each one is listed with what
it cost, because that is what makes the change worth keeping.

### The webhook no longer does the work

`POST /webhooks/whatsapp` was an `async` endpoint that ran the entire agent
turn — a model call of up to thirty seconds, sometimes a Shopify read on top —
before returning 200. Because the turn is synchronous, **one customer's message
blocked the event loop for every other conversation in the process**, and Meta
retries (and eventually disables) a webhook that keeps timing out.

Now the request verifies the signature, claims the message id in its own
committed transaction, downloads any media, and returns 200 in milliseconds.
The turn runs on a worker thread (`chatbot/dispatcher.py`).

The same mechanism **merges fragments**: "عايز هودي" / "أسود" / "لارج" typed
inside a few seconds is now one turn instead of three, which is one model call
instead of three and one reply instead of three.
`MESSAGE_DEBOUNCE_SECONDS`, `MESSAGE_WORKERS`.

### Order status is true again

Staff fulfil orders in Shopify Admin. Nothing told this side, so
`orders.status` never left `Confirmed`, `advance_status` was called by nothing
but the tests, and every message in `notifications.status_change_text` — packed,
shipped, delivered — plus the feedback request was code that **could not run**.
A customer asking "طلبي فين؟" was told `Confirmed` about a parcel that had
arrived the day before.

`POST /webhooks/shopify` now handles `orders/fulfilled`,
`orders/partially_fulfilled`, `fulfillments/update` and `orders/cancelled`:
HMAC-verified, idempotent on `X-Shopify-Webhook-Id`, applied after the response,
and forward-only one stage at a time. `SHOPIFY_WEBHOOK_SECRET`.

**Bug found while building it:** a status push is only *sent* after its
transaction commits, and the callback read `order.status` at that point. One
fulfilment walks Confirmed → Packed → Shipped in a single transaction, so both
callbacks saw the final status — the customer would have been told "on its way"
twice and never told it was packed. `advance_status` now binds the stage at the
time of the transition.

### The catalog can be searched in Arabic

The catalog is entirely English (`Boxy WNS Tee`, `T-Shirts`, `Olive`), and
`get_products` matched raw substrings. `get_products(query="هودي أسود")`
returned **nothing**. It only appeared to work because the model translated
before calling the tool — a habit, not a guarantee, and the first time it does
not translate the shop tells a customer "we don't have that" about something on
the shelf.

`domain/services/search_terms.py` folds Arabic spelling variants (`هودى`/`هودي`,
`أسود`/`اسود`, diacritics, tatweel, Arabic-Indic digits), maps Arabic and
franco words onto the English the catalog uses, and drops the padding a spoken
request carries. Matching is word-start rather than bare substring — `tshirt`
is a substring of `swea·tshirt·s`, which is how "عايز تيشيرت" used to come back
holding hoodies.

### Voice notes and photos are understood

Both used to end the conversation in a handoff. Voice notes are transcribed and
enter the normal pipeline. Photos are read against a shortlist built from the
real catalog, and the reading is handed to the agent as a note — carrying the
product **name**, never its id — that explicitly tells it to verify with the
tools before quoting anything. A `product_id` the shop does not have is
discarded before the caller sees it.

Every failure still falls back to a person, which is what used to happen every
time. `VOICE_NOTES_ENABLED`, `IMAGE_UNDERSTANDING_ENABLED`,
`IMAGE_MATCH_CONFIDENCE`, `LLM_MEDIA_MODEL`. See [docs/MEDIA.md](docs/MEDIA.md).

New handoff reason `voice_received`, so staff can see a message is waiting on
someone to listen rather than reading it as generic `out_of_scope`.

### Product photos can be served from Shopify instead of this repo

This project is a sample built to show the idea, not a deployment tied to one
shop, so it shipped with a scraped photo set bundled in `data/images/` —
fine for a demo, but not what a real deployment should keep doing: every photo
change means a redeploy, and the files add up in the repo for no benefit a
CDN was not already offering for free.

`shopify_catalog.fetch_all` now reads each variant's photo (`image_url`)
alongside the price and stock it already fetched — no extra network call.
`catalog.get_variants` puts it ahead of the matching local file for that
colour once staff attach one in Shopify Admin, without ever inventing a colour
split the local gallery does not already have (`catalog._overlay_images`).
`WhatsAppClient.send_image` sends an `http(s)` path straight to Meta by
`link`, skipping the upload-and-cache path entirely — that path
(`media_id_for`) still exists for genuine local files, chiefly the twelve size
charts and any colour Shopify has no photo for yet. No third image host was
added: the store already holds price and stock, and is the simplest place for
its own photos too. See the "Shopify owns price, stock and orders" section of
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### A paused conversation can actually come back

`request_human` has paused a conversation and written a `StaffQueueItem`
since Phase 1. Nothing ever read that queue back or un-paused the
conversation except the dev harness's `/unpause` stand-in — a customer who
triggered a handoff **stayed stuck** until someone edited the database by
hand. This was a known, documented gap (`docs/ARCHITECTURE.md`, "Known
gap"), not an oversight, but it was still the single biggest thing standing
between this project and a real deployment.

`chatbot/dashboard/` closes it: a staff login (`Staff` already existed —
`domain/services/auth.py`, `python -m backend.cli create-staff` — with
nowhere to log in) at `/dashboard` lists conversations, waiting-on-staff ones
first, oldest wait first. Opening one shows the full history and, if it is
paused, why. Replying sends the customer a real message through the same
`OutboundSender` port the bot's own replies use, then resolves the queue item
and un-pauses the conversation in one transaction; "resolve without a reply"
covers a false alarm or a customer already handled by phone. It refuses
(409) to reply into a conversation the bot still owns — the same two-writers
race `chatbot/dispatcher.py`'s debounce lock exists to prevent.

The session is a signed cookie (`domain/services/auth.py::issue_session_token`),
not a table. With no `DASHBOARD_SESSION_SECRET` set, login refuses outright
(503) — the same call the Shopify webhook makes with no signing secret.
`DASHBOARD_ENABLED`, `DASHBOARD_SESSION_SECRET`, `DASHBOARD_SESSION_HOURS`.

Two small refactors came out of building it: `chatbot/display.py` (stored
history → bubbles a person can read) and `chatbot/media_serving.py` (the
local-file path guard) are now shared between the harness and the
dashboard, rather than living only in the harness.

### The governorate is actually picked

`AGENTS.md` has always said the governorate is "a picked value from a fixed
list, not free text, because it sets the price". It was picked in spirit only:
the model asked in prose and `shipping.resolve` did its best with aliases. What
that costs when the parse is wrong is a shipping fee quoted for the wrong
governorate.

New tool `ask_governorate` (the eighteenth) sends a tappable WhatsApp list —
two steps, region then governorate, because Meta allows ten rows and there are
twenty-seven. A customer who names their governorate themselves still skips
straight past it. `INTERACTIVE_MESSAGES_ENABLED`.

Also: inbound messages are marked read with a typing indicator, so a customer
is not watching an unread message for the length of a model call.

### Security and repository

- **`HARNESS_ENABLED` now defaults to off.** It is an unauthenticated surface
  that can converse as any customer identity; forgetting an environment
  variable must not be what exposes it.
- Dependencies pinned exactly — Railway rebuilds from `requirements.txt` on
  every deploy, and an unpinned range means the build that ships is not the
  build that was tested.
- `pyproject.toml` (ruff + pytest config), `Makefile`, `.editorconfig`,
  `Procfile`, GitHub Actions CI running lint and the suite on both SQLite and
  PostgreSQL, and `docs/`.
- Test suite: 297 → 421 (405 passed, 16 skipped without live Shopify/WhatsApp credentials).
