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
in a modal behind a "دليل المقاسات · Size guide" button.

**Install** (once):

1. Run the script — dry run first, it prints which product gets which chart:

   ```
   python scripts/shopify_size_charts.py
   python scripts/shopify_size_charts.py --apply
   ```

   It needs `DATABASE_URL` pointing at the shop's real database (that is where
   `Product.size_chart` lives) and the `write_files` scope on the Shopify
   token, since uploading a diagram is a file write.

2. Shopify Admin → Online Store → Themes → **⋯ → Edit code**.
3. Under **Snippets**, *Add a new snippet* named `size-chart`, and paste this
   file's contents in.
4. Under **Sections**, open `main-product.liquid` and add one line where the
   button should appear — just after the variant picker block reads well:

   ```liquid
   {% render 'size-chart', product: product %}
   ```

A product with no chart renders nothing at all, so that one line is safe on
every product.

**Re-run the script** after adding a product, changing a chart, or editing
`data/size_charts.json`. It is idempotent: diagrams already in Shopify Files
are reused, and a diagram somebody set by hand in Admin is left alone unless
you pass `--replace-images`.

**Both languages show at once**, deliberately — the shop's customers read
Arabic and English interchangeably, and a chart is glanceable. If the store
later runs Translate & Adapt with two locales, this snippet is the one file to
revisit.
