# Theme snippets

Liquid that belongs to the Shopify theme, kept here rather than only in the
theme editor so it is reviewable and does not exist in exactly one place that
a theme update can overwrite without trace. Nothing in this folder is loaded
by the app — it has to be pasted into the theme.

## `size-chart.liquid`

A bilingual size guide on the product page, from the same charts the bot
sends on WhatsApp.

`scripts/shopify_size_charts.py` puts the data on Shopify as two product
metafields:

| metafield | type | holds |
| --- | --- | --- |
| `custom.size_chart` | file reference | the diagram |
| `custom.size_chart_data` | JSON | the measurements, with Arabic *and* English labels |

The snippet renders the measurements as a table and the diagram beneath it,
in the `<details>` panel that already sits under the product description.

It **replaces** the theme's existing `snippets/size-chart.liquid`, which read
one shop-wide page (`section.settings.size_chart`) for every product. That
page is still honoured as a fallback for a product with no chart of its own,
so nothing that worked before stops working.

**Install** (once):

1. Run the script — dry run first, it prints which product gets which chart:

   ```
   python scripts/shopify_size_charts.py
   python scripts/shopify_size_charts.py --apply
   ```

   It needs `DATABASE_URL` pointing at the shop's real database — that is
   where `Product.size_chart` lives.

2. Shopify Admin → Online Store → Themes → **⋯ → Edit code**.
3. Under **Snippets**, open `size-chart.liquid` and replace its contents with
   this file's.
4. Under **Sections**, open `main-product.liquid` and drop the `if` around the
   render, so the panel no longer depends on a page being configured:

   ```liquid
   {% render 'size-chart', product: product, page: section.settings.size_chart %}
   ```

A product with neither a chart nor a fallback page renders nothing at all, so
that line is safe on every product.

**Re-run the script** after adding a product, changing a chart, or editing
`data/size_charts.json`. It is idempotent: diagrams already in Shopify Files
are reused, and a diagram somebody set by hand in Admin is left alone unless
you pass `--replace-images`.

**Both languages show at once**, deliberately — the shop's customers read
Arabic and English interchangeably, and a chart is glanceable. If the store
later runs Translate & Adapt with two locales, this snippet is the one file to
revisit.
