# 08 — Product database

## Purpose

The single source of truth for what's for sale and how much of it is left — read constantly (every browse, every stock question, every cart action) and written carefully (every order, cancellation, or quantity change).

## Two tables, not one

**A product is what the customer talks about. A variant is what they buy.** "The WANAS hoodie" is a product; "the WANAS hoodie in olive, size M" is a variant, and it's the variant that has a price and a stock count.

The catalog is **18 products / 208 variants, of which 114 are currently in stock.**

### Colour is an axis, not a product

The source store lists `WANAS BLACK HOODIE`, `WANAS GREY HOODIE` and `WANAS OLIVE HOODIE` as three separate products. They are one product in three colours. The seed merges them, which turns **43 source products into 18 real ones without losing a single variant** — 208 before, 208 after.

This matters beyond tidiness:

- **It matches how people shop and how they ask.** "عندكم الهودي؟" is one question about one product, then colour and size are follow-ups. With colour split across products, that conversation needs the bot to know that three catalog entries are the same garment — which it can only do by string-matching names.
- **The catalog stops repeating itself.** Description, size chart, style and category were duplicated across every colour, and could drift apart.
- **Colour becomes filterable and answerable.** "إيه الألوان المتاحة؟" is a field lookup rather than a search across product names.

The merge is an **explicit product-by-product map** in `data/merge_catalog.py`, not a rule over names, because the source handles are actively misleading: `olive-sweatpants` is the black one, `wanas-grey-t-shirt` is the black tee, `wanas-navy-quartrzip-1` is camel brown. A regex over handles would silently mis-file stock. The script also refuses to run if any source product is left out of a group, so a new product can't be quietly dropped.

### Why stock lives on the variant

A product-level count cannot express this catalog. Availability stored as separate size and colour lists says the Cairokee T-shirt comes in XL/Brown, because XL is available (in Black) and Brown is available (in S/M/L). That variant is not for sale.

**Every axis combination gets a row, and `stock_qty` alone decides buyability.** 94 of the 208 rows sit at zero — including combinations the store lists but has never stocked. They're kept rather than deleted so the bot can say "XL only comes in Black" instead of pretending the combination was never offered. A row existing never means it can be sold.

### Product fields

| Field | Type | Notes |
|---|---|---|
| `product_id` | Primary key | Our own slug, e.g. `wanas-hoodie`. **Not a Shopify handle** — one product now spans several |
| `name` | Text | Stored in its natural Latin-script form and never translated (see `02-chatbot.md`) |
| `category` | Text | One of six — see the taxonomy below |
| `department` | Text | `unisex` (16) or `women` (2) |
| `style` | List of text | Cut and construction: `oversized`, `boxy-fit`, `ringer`, `zip-through`, `quarter-zip`, `crewneck`, `pullover`, `knitted`, `wide-leg`, `lightweight`, `graphic`, `worker`, `fitted` |
| `collection` | Text, nullable | `WINTER COLLECTION` (7) or `CAIROKEE MERCH` (3). **Null for 8** — see below |
| `size_chart` | Text, nullable | Which chart in `data/size_charts.json` applies |
| `sizes` / `colors` / `lengths` | Lists of text | **Display summaries derived from the variants.** Never the basis of an availability decision |
| `price` | Number | The **lowest** variant price. Not quotable on its own — three products cost more in one colour |
| `original_price` | Number | The **highest pre-discount** price across the variants. A strike-through number, not a price anyone pays. It is not the top of a range |
| `on_sale` | Boolean | True if any variant is discounted |
| `images` | List of file paths | Every photo of the product, under `data/images/` |
| `color_images` | Map | Colour → its own photos. This is what lets a reply about the olive one show the olive one |
| `description` | Text | Short plain-text blurb, usually including the model's size and height |
| `source_products` | List | Provenance: the source handle, Shopify ID and colour each variant group came from. Kept for re-syncing against the store |

### Variant fields

| Field | Type | Notes |
|---|---|---|
| `variant_id` | Primary key | e.g. `wanas-hoodie-m-olive`. **The only thing that can be added to a cart** |
| `product_id` | Foreign key | |
| `size` | Text | Present on every variant |
| `color` | Text | Present on all 208 variants. Nullable in the schema only for a future colourless product |
| `length` | Text, nullable | `Long` / `Short`, Worker Jacket only |
| `price` / `original_price` / `on_sale` | Number / bool | Per variant |
| `stock_qty` | Integer | **The only field that is decremented, and the only one that can race** |
| `low_stock_threshold` | Integer | Seeded at 2; editable per variant |
| `status` | Computed | in stock / low stock / sold out |

**Colour and length stay separate columns** rather than one generic "option 2". They're different questions to the customer — "which colour?" and "long or short sleeve?" — and a single column can't say which one is still unanswered. The Worker Jacket carries both, and its variants are size × colour × length.

## The category taxonomy

Six categories, modelled on how ASOS structures menswear:

| Category | Products | Variants |
|---|---|---|
| T-Shirts | 5 | 48 |
| Hoodies & Sweatshirts | 6 | 68 |
| Polo Shirts | 2 | 28 |
| Joggers & Sweatpants | 2 | 24 |
| Jackets | 1 | 16 |
| Tops | 2 | 24 |

**Crewnecks and quarter-zips are not categories.** No major retailer treats them as one, and neither should this: they're all fleece-backed sweats and they live under `Hoodies & Sweatshirts`, with the difference carried in `style`. Splitting them out produced categories with one product in them, which is a taxonomy that describes the current stock rather than the shop.

**`style` is a filter, not a category** — the same role fit and cut play as facets on a large retailer. It answers "عايز حاجة oversized" without needing a category per cut, and it survives new products that combine cuts.

**`department` exists because two products are cut for women** and everything else is unisex. Without it, "عندكم حاجة حريمي؟" has no answer that isn't a guess from the product name.

## Collections are a separate, optional axis

`WINTER COLLECTION` and `CAIROKEE MERCH` are the only two. Eight products belong to neither, and that's correct rather than incomplete.

A collection is a merchandising decision that changes every season; a category is what the garment is and doesn't change at all. Large retailers keep these strictly apart — ASOS splits "shop by product" from "shop by edit", and its edits are separate content entities with their own URLs so they can be relabelled without touching the taxonomy. The old seed mixed them, which produced a `T-SHIRTS` collection sitting alongside a `T-Shirt` product type and a Worker Jacket filed under `T-SHIRTS`.

**Nothing may depend on `collection` to find a product.** It's for browsing and campaigns only.

## Size charts

`data/size_charts.json` holds the published charts; `size_chart` on a product points at one. **12 charts, and all 18 products are mapped.**

| Chart | Products | Measurements |
|---|---|---|
| `ringer-boxy-tee` | Ringer Tee, Boxy WNS Tee | Width, length |
| `oversized-graphic-tee` | Cairokee Tee, Cairokee Tee 2, Envy Tee | Width, length |
| `oversized-hoodie` | WANAS Hoodie, Cairokee Hoodie | Width, length |
| `wanas-zip-hoodie` | WANAS Zip-Hoodie | Chest width, total length |
| `zipup` | Zipup | Chest width, total length, **arm length** |
| `oversized-crewneck` | WANAS Crewneck | Chest width, total length |
| `quarter-zip` | WANAS Quarter-Zip | Chest width, total length |
| `oversized-polo` | WANAS Polo | Chest width, total length |
| `knitted-polo` | Knitted Polo | Width, length |
| `wide-leg-sweatpants` | WANAS Sweatpant, Lightweight Sweatpant | Waist, total length, leg opening |
| `worker-jacket` | Worker Jacket | Chest width, total length, **short sleeve, long sleeve** |
| `wns-tops` | Feelin Fine Top, Heart Top | Length, width — **S/M/L only, no XL** |

Each chart carries its per-size rows, the image to send the customer (`data/size-charts/`), and its measurement labels in both English and Arabic.

**Measurements are garment-flat, not body measurements.** A size S sweatpant waist of 31 cm is the waistband laid flat, roughly half the way around. A customer reading it as a body measurement concludes the trousers are for a child, so this has to be said whenever numbers are quoted.

**Charts are assigned per product in the merge map, not derived from `category`.** Cut doesn't follow category, and a rule would silently get it wrong:

- `T-Shirts` splits across two charts — the ringer/boxy tees and the oversized graphic tees are different patterns.
- `Polo Shirts` splits across two — knitted and WANAS polos are cut differently.
- `Hoodies & Sweatshirts` splits across five.

A near-enough chart is worse than no chart: it produces confident, precise, wrong numbers.

**`worker-jacket` is the only length-aware chart.** It carries both a short-sleeve and a long-sleeve measurement, matching the `length` axis on those variants — so the answer depends on which length the customer picked, not just the size.

**Null `size_chart` remains a supported state** even though nothing is null today. New products arrive before their charts do, and everything reading this field must handle "no chart" by saying so — see `02-chatbot.md`. Adding a chart means an image in `data/size-charts/`, an entry in `size_charts.json`, and a line in the map.

## How `status` is computed

Per variant, not stored — derived from `stock_qty` vs `low_stock_threshold` vs zero, calculated at read time (or refreshed on every stock change) so it's never stale relative to the actual number:
- `stock_qty = 0` → sold out
- `0 < stock_qty <= low_stock_threshold` → low stock
- `stock_qty > low_stock_threshold` → in stock

## How it's written to

- **Inventory service exclusively** (see `01-backend-platform.md`) — every decrement (new order) and increment (cancellation, timeout release) goes through its atomic "decrement if enough stock exists" operation. No other service writes `stock_qty` directly, which is what prevents race conditions.
- **Admin dashboard** — staff edit product details, prices, thresholds, per-variant stock, and `color_images` directly (not through the Inventory service's order-driven path).

## How it's read

- Website catalog and cart (live stock per item)
- Every DM channel's stock-question handling
- Order service's two stock checks (initial add-to-cart, and the final re-check at confirmation)
- Analytics (best-sellers, turnover, low-stock frequency)
- **Image Recognition component** — reads the local `images` across the whole catalog to compare against a customer-submitted photo. `color_images` narrows a match to the right colourway rather than just the right garment

## Edge cases worth knowing about

- **Threshold crossed exactly at zero** — sold out always fires regardless of threshold value, so a variant with no threshold set still correctly shows sold out at zero.
- **Staff manually adjusts stock while an order is mid-checkout** — the final re-check at confirmation (not just the initial add-to-cart check) is what catches this, since it reflects whatever the number is right before commit.
- **A product has stock but the requested variant doesn't** — the normal case, not the exception: **14 of the 17 products with any stock are only partly buyable.** The customer is told that variant specifically is out and offered the siblings that aren't, rather than a blanket "sold out." After the colour merge this happens more often per product, not less, because one product now covers what used to be three.
- **Every variant of a product hits zero** — the product is sold out; there's no separate product-level flag to fall out of sync with. Only the Cairokee Hoodie is currently in this state.
- **A source handle doesn't describe the product** — the store has several misleading ones (`olive-sweatpants` is the black lightweight sweatpant, `wanas-navy-quartrzip-1` is camel brown). They survive only inside `source_products` for re-syncing. Never derive colour or anything customer-facing from them; the colour is on the variant.
- **A variant exists but was never really for sale** — the source store lists combinations it doesn't stock. They're kept as rows with `stock_qty = 0` rather than deleted, so the bot can say "XL only comes in Black" instead of pretending the combination was never offered.
