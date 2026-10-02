# Rehla

**Rehla** (رحلة) is a women's clothing brand. This repo is its chatbot. It runs
against Shopify when `SHOPIFY_*` is set and **without it** otherwise (local shelf).
The code began as a fork of an earlier streetwear bot, kept in the archive repo
at tag `wanas-v1`; nothing here depends on it.

## Edit these first

| What | Where |
|---|---|
| Shipping fees (70 for Cairo/Giza, 85 elsewhere), delivery 3–5 days, COD, exchange/return days | `domain/services/shop_facts.py`, the block marked `REHLA` |
| Catalog | `python scripts/rehla/build_seed.py` rebuilds `data/products_seed.json` and `data/images/` from `data/rehla_source/products_clean.json`. Category, style, sleeve and name fixes live in `CURATED` in that script |
| Voice, brand, rules | `assistant/prompt.py` |
| Arabic search words / garments we don't sell | `domain/services/search_terms.py` / `domain/services/garments.py` |

Fees only fill **empty** rows at boot. Once a DB has fees, change them in the
dashboard or with `python manage.py set-fee`.

## No Shopify: the local store

With `SHOPIFY_STORE_DOMAIN`/`SHOPIFY_ADMIN_TOKEN` empty (and `LOCAL_STORE`
not `0`), `integrations/shopify/local_shelf.py` makes the database the shelf:
- Price and stock come from `variants`.
- `confirm_order` writes the order locally (reference `RHL-1001`, …),
  decrements local stock and sends the normal confirmation. No `orderCreate`.
- Product import/reconcile and webhook registration only run when Shopify is
  configured, so they're off.
- Post-order edits that need Shopify (quantity change / swap / add on a placed
  order) answer "store unavailable", so staff handle them. Cancelling before
  shipping works locally and restocks.

## Data notes

- 21 products, 204 variants. Stock is 10 per available variant and 0 per
  sold-out one, because the storefront only exposes available/sold out.
- SKU = the store SKU where it exists and is unique. Otherwise it's
  `handle-colour-size` (4 products had none, and one SKU was shared by two variants).
- Colour typos fixed for display (Burghandy→Burgundy, Lavendar→Lavender,
  Violent→Violet, emoji dropped). Titles with typos got clean names (see `CURATED`).
- **Rehla Jacket**: the scrape caught 100 EGP (compare-at 1200); the live store sells it
  at 1000. The seed now says 1000 (`price` in `CURATED`). A database seeded before
  this still holds 100 -- harmless while Shopify answers (its price wins), but it is
  the number quoted if Shopify is unreachable, so correct it in the dashboard.
- No size charts were published. The bot hands off with `size_help` ("we'll send you the chart, and a team member will help").
- Images were re-encoded to JPEG ≤1280px (219 MB → 19 MB) so every one is under WhatsApp's 5 MB limit.

## Try it locally

```bash
python scripts/rehla/demo_conversations.py --fresh   # 5 scripted conversations, real LLM from .env
pytest tests/test_rehla.py
```

## WhatsApp templates (outside the 24-hour window)

Meta only allows an approved template to a customer who has not written in the
last 24 hours. The code reads the names from these variables and defaults to
the Wanas-era names that are approved today; set a variable to switch, set it
to an empty value to turn that template off. No code change, no redeploy beyond
the variable.

| Variable | Default (today) | Rehla template to create |
|---|---|---|
| `WHATSAPP_TEMPLATE_ORDER_UPDATE` | `wanas_order_update` | `rehla_order_update` |
| `WHATSAPP_TEMPLATE_FEEDBACK_REQUEST` | `wanas_feedback_request` | `rehla_feedback_request` |
| `WHATSAPP_TEMPLATE_ORDER_CONFIRMATION` | `wanas_order_confirmation` | `rehla_order_confirmation` |
| `WHATSAPP_TEMPLATE_BACK_IN_STOCK` | `wanas_back_in_stock` | `rehla_back_in_stock` |
| `WHATSAPP_TEMPLATE_LANGUAGE` | `ar` | `ar` |

Create each as **Custom**, language `ar`. Utility: order update, feedback,
confirmation. Marketing: back in stock. Texts (the replies
the customer sends back open the 24-hour window):

1. `rehla_order_update` (Utility): «في تحديث على طلبك من رحلة ✅» / «رد على الرسالة دي وهنقولك الطلب وصل لفين وكل التفاصيل على طول.» (optional quick reply: «طلبي وصل فين؟»)
2. `rehla_feedback_request` (Utility): «طلبك من رحلة وصلك ✅» / «تقيّمي تجربتك معانا من 1 لـ 5؟ ولو عندك أي ملاحظة اكتبيها في ردك — بتفرق معانا فعلاً.»
3. `rehla_order_confirmation` (Utility): «وصلنا طلبك واتأكد ✅» / «رد على الرسالة دي وهنبعتلك تفاصيل الطلب والإجمالي وموعد الوصول.»
4. `rehla_back_in_stock` (Marketing): «خبر حلو 🖤» / «القطعة اللي كنتي مستنياها رجعت متوفرة تاني في رحلة.» / «ردي على الرسالة دي وهنظبطلك المقاس واللون قبل ما تخلص تاني.» (optional quick reply: «عايزة أطلبها»)

There is no abandoned-cart template: the idle-cart nudge is nudge #2 of the
"customer went silent" follow-up below, free-form inside the 24-hour window
only.

Then set the variables on Railway to the `rehla_*` names once each shows
**Approved**. A name Meta has not approved is worse than an empty one.

## Customer went silent: follow-up nudges

`assistant/silence_nudges.py`, polled by the scheduler every
`NUDGE_POLL_SECONDS` (60). At most **two** per silence; her next message
resets the counter.

1. **Nudge #1** -- `NUDGE_FIRST_MINUTES` (10) after her last message, on any
   conversation the bot answered. One short line written by the chat model
   from the conversation («لسه معانا؟ ...» naming what she was looking at);
   a fixed line if the model is down or writes a number, a long paragraph or
   a non-Egyptian word.
2. **Nudge #2** -- `NUDGE_SECOND_HOURS` (2) after the same message, **only**
   with a cart still open (the old abandoned-cart wording, naming what is in
   it). A general chat gets none.

Never sent: outside the 24-hour window (free-form only, no template), while a
conversation is handed off or was last answered by staff, once an order was
placed, after she closed the chat («شكراً», «سلام», «مش عايزة»), or after she
asked to stop («stop», «متبعتليش») -- remembered past this silence. Cairo quiet
hours `NUDGE_QUIET_START_HOUR`-`NUDGE_QUIET_END_HOUR` (0-9): #1 is dropped, #2
waits for 09:00 if the window is still open then.

Wording is gender-neutral («حضرتك»), matching the bot's own rule never to
assume who is buying. State is the `silence_nudges` table (created at boot),
so a redeploy resumes where it left off; each step is claimed by a conditional
UPDATE committed before the send, so several workers send it once at most.

## Shopify

The store is the source of truth for price, stock, orders and size charts.
`scripts/shopify_relink_skus.py` (dry-run by default) makes the database's
variant ids equal the store's SKUs. `docs/shopify_cutover.md` records what
was found and done when the bot was connected to the store.
