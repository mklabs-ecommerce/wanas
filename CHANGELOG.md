# Changelog

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

`backend/services/search_terms.py` folds Arabic spelling variants (`هودى`/`هودي`,
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
- Test suite: 297 → 397 (381 passed, 16 skipped without live Shopify/WhatsApp credentials).
