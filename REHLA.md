# Rehla branch

This branch is the Wanas bot re-skinned for **Rehla** (رحلة), a women's
clothing brand. It runs **without Shopify**. Branched from tag `wanas-v1`.
The checkpoint and how to restore Wanas are in `S:\E-commerce\_checkpoint\RESTORE.md`.

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
- **Rehla Jacket is priced 100 EGP (compare-at 1200) in the store data.**
  That looks like a data error. Fix it in the dashboard before customers see it.
- No size charts were published. The bot hands off with `size_help` ("we'll send you the chart, and a team member will help").
- Images were re-encoded to JPEG ≤1280px (219 MB → 19 MB) so every one is under WhatsApp's 5 MB limit.

## Try it locally

```bash
python scripts/rehla/demo_conversations.py --fresh   # 5 scripted conversations, real LLM from .env
pytest tests/test_rehla.py
```
