# 15 — Tool contracts

The exact arguments and return shapes for every tool the agent can call. `02-chatbot.md` covers *why* the tools work this way; this file is what you build against.

## Rules that apply to every tool

- **Returns a JSON object, never a string of prose.** The model does the phrasing.
- **Never raises.** A failure returns `{"error": "<code>", ...}`. A crash inside a tool ends the customer's conversation, which is a worse outcome than any error message.
- **`error` is a stable code**, not a sentence. The model maps codes to wording; changing a code changes behaviour, changing wording doesn't.
- **The channel identity is injected by the runtime**, never passed by the model. Any tool that touches a cart, an order, or a client reads `channel` + `external_id` from the request context. See `02-chatbot.md`.
- **Money is a number in EGP**, never a formatted string.
- **Unknown or extra arguments are rejected** with `{"error": "bad_arguments", "detail": "..."}` rather than ignored.

### Shared shapes

```
Variant       { variant_id, size, color, length, price, original_price,
                on_sale, stock_qty, status }
CartLine      { line_id, variant_id, product_name, size, color, length,
                quantity, unit_price, unit_original_price, line_total }
Cart          { lines: [CartLine], item_count, subtotal }
```

`status` is `"in_stock" | "low_stock" | "sold_out"`, computed per `08-product-database.md`. **`stock_qty` is returned to the model but must not be quoted to the customer** — exact counts invite haggling and go stale; `status` is what a reply may reference.

---

## Catalog

### `get_categories()`

No arguments. The opening move — grounds the model in what actually exists.

```json
{
  "categories": [
    { "category": "T-Shirts", "product_count": 5 },
    { "category": "Hoodies & Sweatshirts", "product_count": 6 }
  ],
  "styles": ["oversized", "boxy-fit", "zip-through", "..."],
  "departments": ["unisex", "women"],
  "collections": ["WINTER COLLECTION", "CAIROKEE MERCH"]
}
```

**Categories come first and collections last, labelled as optional.** 8 of the 18 products have no collection, so a model that opens by offering collections has hidden nearly half the shop.

### `get_products(category?, style?, department?, collection?, query?)`

All arguments optional; with none, returns everything. `query` is free text matched against `name`, `category`, `style` and variant colours together — that's what resolves "الهودي الزيتي" now that olive is a colour rather than part of a name.

```json
{
  "products": [
    {
      "product_id": "wanas-hoodie",
      "name": "WANAS Hoodie",
      "category": "Hoodies & Sweatshirts",
      "style": ["oversized", "pullover"],
      "department": "unisex",
      "collection": "WINTER COLLECTION",
      "colors": ["Black", "Grey", "Olive"],
      "sizes": ["S", "M", "L", "XL"],
      "price_from": 650,
      "price_to": 700,
      "original_price_to": 900,
      "on_sale": true,
      "any_in_stock": true,
      "description": "Oversized pullover hoodie. Model wears M (70kg, 178cm)."
    }
  ],
  "count": 1
}
```

**`price_from` / `price_to` are `min` and `max` of the variants' *current* prices** — computed by the tool, not read from the product record. Three products are priced per colour: the WANAS Hoodie is 650 in black and olive but 700 in grey. A single product-level number would have the model quote 650 for a grey hoodie the customer is then charged 700 for, at the door, in cash. Where `price_from == price_to`, say one number; otherwise say "from 650".

**Don't confuse `price_to` with the product's `original_price` field**, which is the highest *pre-discount* price (900 for that hoodie) and is a strike-through number, not a price anyone pays.

`colors` and `sizes` here are the **full run, including sold-out ones** — they're for describing the product, not for offering. Use `get_variants` before offering anything.

### `get_variants(product_id, more_images?)`

```json
{
  "product_id": "wanas-hoodie",
  "name": "WANAS Hoodie",
  "description": "Oversized pullover hoodie. Model wears M (70kg, 178cm).",
  "has_size_chart": true,
  "variants": [
    { "variant_id": "wanas-hoodie-s-olive", "size": "S", "color": "Olive",
      "length": null, "price": 650, "original_price": 900, "on_sale": true,
      "stock_qty": 10, "status": "in_stock" },
    { "variant_id": "wanas-hoodie-m-olive", "size": "M", "color": "Olive",
      "length": null, "price": 650, "original_price": 900, "on_sale": true,
      "stock_qty": 0, "status": "sold_out" }
  ],
  "in_stock": ["wanas-hoodie-s-olive", "wanas-hoodie-s-black"],
  "images": ["data/images/wanas-olive-hoodie/01.jpg"],
  "color_images": { "Olive": ["data/images/wanas-olive-hoodie/01.jpg"] }
}
```

- **Sold-out variants are returned too**, so the bot can say "XL only comes in Black" rather than pretending the combination never existed. `in_stock` is the offerable subset.
- **Errors:** `{"error": "product_not_found", "product_id": "..."}`.
- **`color_images` may be empty** for the five products the store never split by colour (both Tops, the three Cairokee items). Fall back to `images` — an unlabelled photo is fine; the wrong colourway labelled confidently is not.
- **Image policy lives in the tool layer, not just the prompt** (`chatbot/tools/base.py`). A plain call attaches exactly **one** photo, and never a photo already sent earlier in the same conversation (tracked via the `attachments` recorded on the agent's own past replies, see `chatbot/messages.py`) — if the product was shown before, a plain call attaches nothing new. `more_images: true` is the only way to get more: up to **two** additional photos, preferring colours not shown yet, only repeating one already sent if nothing else exists. The model is instructed to only ever pass `more_images: true` for an explicit "show me more" from the customer.
- **Repeated identical calls in one conversation are served from what was already answered** (`chatbot/tools/base.py:_cached_result`), not re-fetched — applies to `get_categories`, `get_products`, `get_variants`, `get_size_chart` and `get_shipping_fee` only; every tool with a side effect always runs for real.

---

## Cart

### `add_to_cart(variant_id, quantity?)`

`quantity` defaults to 1, must be ≥ 1, capped at **10 per line** (above that it's a wholesale enquiry, and it's the cheapest guard against a typo emptying the shelf).

Success returns the updated `Cart`. Refusals:

```json
{ "error": "out_of_stock",
  "variant": { "variant_id": "wanas-hoodie-m-olive", "size": "M", "color": "Olive" },
  "alternatives": [
    { "variant_id": "wanas-hoodie-s-olive", "size": "S", "color": "Olive" },
    { "variant_id": "wanas-hoodie-s-black", "size": "S", "color": "Black" }
  ] }
```

- `alternatives` are **in-stock siblings of the same product** — same colour in other sizes first, then the same size in other colours. **They are the only substitutes the model may offer;** anything else would be invented.
- `{"error": "insufficient_stock", "available": 3}` when the quantity exceeds what's left.
- `{"error": "variant_not_found", "variant_id": "..."}` — never resolve to the nearest match. Silently correcting an identifier ships the wrong size.

### `view_cart()` → `Cart`

### `remove_from_cart(line_id? | variant_id? | clear_all?)`

Exactly one argument. Returns the updated `Cart`, or `{"error": "bad_arguments"}` if none or several were given. Removing a line that isn't there returns the cart unchanged rather than an error — the customer's intent is already satisfied.

---

## Sizing and shipping

### `get_size_chart(product_id)`

```json
{
  "has_chart": true,
  "chart_id": "wide-leg-sweatpants",
  "title": "Wide-leg sweatpants",
  "unit": "cm",
  "measurement_note": "Garment measurements laid flat, not body measurements.",
  "length_specific": false,
  "measurements": [
    { "key": "waist", "label_en": "Waist", "label_ar": "الوسط" }
  ],
  "sizes": { "S": { "waist": 31, "total_length": 106, "leg_opening": 30 } },
  "image": "data/size-charts/wide-leg-sweatpants.png"
}
```

- **No chart** → `{"has_chart": false, "product_id": "..."}` **and nothing else**. Returning a neighbouring product's chart is the failure this shape exists to prevent.
- **`length_specific: true`** (Worker Jacket only) means the sleeve measurement depends on which length the customer picked. Its `measurements` carry `applies_to_length`, and the bot asks Long or Short before quoting a sleeve.
- **`sizes` may not have four keys** — the Tops are S/M/L. Never extrapolate a missing size.
- The runtime attaches `image` to the outgoing reply; the model states the numbers in text as well, for anyone who never opens the picture.
- **`measurement_note` and `length_specific` are supplied by the tool, not stored per chart.** `size_charts.json` carries `length_specific` only where it's true, and no note at all — the tool fills both in so every chart answers the same shape.

### `get_shipping_fee(governorate)`

```json
{ "governorate": "Cairo", "fee": 60 }
```

`{"error": "no_rate_set", "governorate": "..."}` when the shop hasn't priced that governorate — and an order for it **cannot** be confirmed. `{"error": "unknown_governorate", "valid": [...]}` when the value isn't in the list at all; the list is the source of truth, not free text.

---

## Ordering

### `confirm_order(customer_name, governorate, address, contact_phone, email?)`

The only tool that writes an order. Runs the atomic transaction in `01-backend-platform.md`.

```json
{
  "order_id": "WNS-1042",
  "status": "Confirmed",
  "payment_method": "cash_on_delivery",
  "items": [ { "product_name": "WANAS Hoodie", "size": "S", "color": "Olive",
               "length": null, "quantity": 1, "unit_price": 650,
               "unit_original_price": 900 } ],
  "subtotal": 650, "discount_amount": 0, "shipping_fee": 60, "total": 710
}
```

Refusals — **all checked inside the tool, not trusted from the conversation**:

| Error | When |
|---|---|
| `cart_empty` | Nothing to order |
| `missing_fields` + `fields: [...]` | Any required argument absent or blank. It does not infer a governorate from the address text |
| `no_rate_set` | The governorate has no shipping fee |
| `items_out_of_stock` + `items: [...]` | The final live re-check failed. Nothing is written |
| `client_blocked` | `clients.status = blocked` |

**The model must not tell the customer an order was placed until this returns an `order_id`.** A confirmation the backend didn't record is the worst failure available here.

`email` is optional in Phase 1 — see `07-client-database.md`.

---

## After the order

### `get_my_orders(include_closed?)`

Open orders by default. This is what lets the bot ask "which order?" instead of guessing by recency.

```json
{ "orders": [ { "order_id": "WNS-1042", "status": "Packed", "placed_at": "...",
                "total": 710, "modifiable": true,
                "items": [ { "variant_id": "wanas-hoodie-s-olive",
                             "product_name": "WANAS Hoodie",
                             "size": "S", "color": "Olive", "quantity": 1 } ] } ] }
```

**`modifiable` is computed by the tool**, not derived by the model from `status`. One place owns the rule.

### `modify_order_quantity(order_id, variant_id, quantity)`

Sets an existing line to an absolute quantity, 0–10 (the same cap as `add_to_cart`). `quantity: 0` removes the line; removing the last line is refused (`would_empty_order` — cancel instead, so the order doesn't become an empty ghost).

- **Increases go through the Inventory service's atomic decrement**, exactly like a new order, and fail with `insufficient_stock` if it isn't there. Decreases return stock.
- **`subtotal` and `total` are recomputed. `shipping_fee` is not re-quoted** — it was copied at order time and stays copied, even if the rate table changed since.
- Appends to `modification_log`: what changed, when, and via which channel.
- Returns the updated order **including the new total**, and the model must read the new total back to the customer. Cash on delivery means a silently changed amount becomes an argument at the door.
- Refusals: `order_not_found`, `not_modifiable` (with `status`), `line_not_found`, `insufficient_stock`, `would_empty_order`.

### `cancel_order(order_id)`

Same status rule, returns all stock, notifies staff. Refusals: `order_not_found`, `not_modifiable`.

### `request_item_swap(order_id, from_variant_id, to_variant_id?, note?)`

**Queues a request. Never applies the swap.** Staff have to check stock for the replacement and decide.

```json
{ "queued": true, "request_id": "SWAP-88" }
```

The model must not imply the swap is done — the honest reply is that someone will confirm. `to_variant_id` is optional because the customer may only have described what they want.

### `submit_feedback(order_id, rating, text?)`

`rating` is 1–5. Only accepted for a `Delivered` order, and only one per order (`already_rated`). A rating alone is enough; `text` is optional on top.

Without this tool the bot cannot record the star rating it asks for after delivery, which is in Phase 1's definition of done.

---

## Escalation

### `request_human(reason, summary)`

The single way a conversation leaves the bot. `reason` is one of `unclear`, `complaint`, `customer_asked`, `image_received`, `out_of_scope`.

```json
{ "queued": true, "conversation_paused": true }
```

- Sets a **pause flag on the channel identity**. While it's set the runtime stops calling the model for that conversation entirely — incoming messages are stored and shown to staff, not answered.
- **Cleared only by a staff action in the dashboard.** Not by a timer, and not by the model deciding the conversation looks normal again: the whole point is that a human is now handling it.
- `image_received` is raised by the **runtime**, before the model sees anything. In Phase 1 the model is never given an image (`14-image-recognition.md`), so it can't classify one.
- A `complaint` about a delivered order goes here, never to an offer of a new order — see `02-chatbot.md`.

### `get_my_profile()`

What the runtime knows about whoever is messaging. Called before asking for shipping details, so a returning customer is shown their address rather than asked to retype it.

```json
{
  "known": true,
  "client_id": "c_812",
  "full_name": "…", "phone": "…", "email": null,
  "governorate": "Cairo", "address": "…",
  "pending_link": null
}
```

- **`known: false`** for a channel identity with no `client_id` yet — the normal state for a first-time customer, and the case every implementation forgets. There is no error; an unknown customer is not a failure.
- **`pending_link`** is set when the phone or email just given at checkout exactly matches an existing client: `{"client_id": "c_640", "matched_on": "phone", "masked_name": "M… A…"}`. That's the signal for the bot to ask "Is this you?" and then call `link_client`. Without it the identity-link path in `07-client-database.md` can never be reached.
- The name is masked because an unconfirmed match is, by definition, possibly a stranger. Showing a full name before confirmation leaks it to whoever typed the number.

### `link_client(confirmed)`

Called only after the customer answers the "Is this you?" question raised when a phone or email matches an existing client (`07-client-database.md`). `confirmed: true` attaches this channel identity to the existing `client_id`; `false` leaves them separate and creates a fresh record at checkout.

Nothing is linked without this call. An automatic link is the same operation as showing one customer another customer's address and order history.

---

## Seventeen tools, and why the count matters

`get_categories`, `get_products`, `get_variants`, `add_to_cart`, `view_cart`, `remove_from_cart`, `get_size_chart`, `get_shipping_fee`, `confirm_order`, `get_my_orders`, `modify_order_quantity`, `cancel_order`, `request_item_swap`, `submit_feedback`, `request_human`, `get_my_profile`, `link_client`.

Every capability the bot has is on this list. If a behaviour is described anywhere in these docs and has no tool here, **the bot cannot do it** — that's the check to run whenever a doc adds a capability, and it's the check that caught feedback, human handoff, identity linking and profile lookup being described in prose with nothing behind them.
