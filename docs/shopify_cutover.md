# Shopify cutover (2026-10-01)

The bot and the dashboard were running on the local database (`LOCAL_STORE=1`,
no `SHOPIFY_*` on Railway). This records what was found when they were
connected to the Rehla store, and what was done.

## What was found

| | Store (`p0hd05-m5.myshopify.com`) | Production database (`rehla-db`) |
|---|---|---|
| Products / variants | 21 active / 204 | 21 / 204, 0 orders, 0 clients, 4 conversations |
| Scopes | all required scopes granted | n/a |
| SKUs | every variant has one; **one SKU (`BO-2101378`) was held by two variants** (flares-gray-L and squared-gray-L) | 51 variants had generated ids (`handle-colour-size`) that are not store SKUs |
| Size charts | **no** `custom.size_chart` / `size_chart_data` metafield on any product (four products carry a size table inside the description HTML) | 0 `size_charts` rows |
| Webhooks | 8 subscriptions already registered on the production domain: orders create / updated / fulfilled / partially fulfilled / cancelled, fulfillments update, products create / update | n/a |
| Prices | Rehla Jacket is 1000 (the local seed said 100) | stock/price in the DB were seed values |
| Railway variables | no `SHOPIFY_*`, `LOCAL_STORE=1`, no `SHOPIFY_WEBHOOK_SECRET`, no `WHATSAPP_TEMPLATE_*`, no `RESEND_FROM` | `DATABASE_URL` is the Railway Postgres (`postgres-cgxu`, database `railway`) |

## What was done

1. **Shopify writes (2, dry-run first):** `BO-2101378` on the two variants became
   their catalogue ids (`rehla-flares-long-sleeves-top-gray-l`,
   `rehla-squared-long-sleeves-top-gray-l`) via `scripts/shopify_relink_skus.py`.
2. **Railway variables:** `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`,
   `SHOPIFY_API_VERSION` set from the local `.env`; `LOCAL_STORE` removed.
   (A first attempt piped the values in through PowerShell and stored them with
   a leading byte-order mark, which made every Shopify call fail on a non-ASCII
   URL. They were re-set as arguments and verified to start with a letter.
   Do the same if you ever set them from PowerShell.)
3. **Boot import:** with Shopify configured, boot mirrored the four products whose
   SKUs the database did not know (`imported 4 product(s)`). They arrived as
   `Uncategorized` / `unisex` with no style or sleeve. `scripts/shopify_merge_adopted.py`
   (dry-run first) copied the curated category, department, style and sleeve onto the
   adopted rows and **archived** (not deleted) the four old rows. Production now
   holds 25 products, 4 of them archived.
4. **Seed:** `data/products_seed.json` carries the store's SKUs for those 51 variants,
   so a fresh database starts consistent with the store.
5. The production database was **not** re-keyed: step 3 made it unnecessary.

## Verified (production, read through the same code the bot runs)

* `/health`: `shopify_configured` true, no missing scopes, WhatsApp and Instagram
  configured, webhooks configured for both.
* Price and stock of three variants of one product equal Shopify's.
* A price changed on Shopify (600 → 611) was seen by the bot, then restored.
* Product photos are `cdn.shopify.com` URLs (served as `image/jpeg`).
* A test order for `TEST` created Shopify order #1043, took stock 100 → 99,
  and cancelling it returned stock to 100. Its two alerts were resolved. It left a
  cancelled `RHL-1001` row and a `TEST` customer in Shopify (a customer cannot be removed from here).
* Dashboard (as `rehla-admin`, session issued server-side; no password used): products,
  orders, customers, stats, insights, queue and settings answered 200.
* Two paused Instagram conversations were released with `manage.py release-conversation`,
  which sends nothing to the customer.

## Still open

* `SHOPIFY_WEBHOOK_SECRET` is not set, so Shopify's webhooks are refused
  (`shopify_webhooks_configured` is false) and no packed / shipped / delivered
  message fires. The secret is the **client secret of the custom app that owns the
  Admin token**, not the token: Shopify Admin → Settings → Apps and sales channels →
  Develop apps → *the app* → API credentials → "API secret key" (in the Dev Dashboard:
  the app → Settings → Client secret). Set it as `SHOPIFY_WEBHOOK_SECRET` on Railway.
* There are no size charts on Shopify. Until staff add them (dashboard, or the
  `custom.size_chart` metafield and `scripts/shopify_size_charts_import.py`), the bot
  hands off with `size_help`.
* `RESEND_FROM` is unset: owner alert emails go out from Resend's sandbox sender and
  reach only the Resend account owner (`alert_email_deliverable` is false).
