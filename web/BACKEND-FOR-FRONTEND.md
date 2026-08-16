# Wanas Gallery — what the backend gives the web store

Everything a frontend needs to know, with no opinion about how it looks. Build
whatever you like on top; these are the shapes, the rules, and the places where
getting it wrong costs money.

**The store is a separate phase.** The backend already exists for the WhatsApp
bot and the admin dashboard, and the store is another client of it — the same
order transaction, the same stock, the same rules. Nothing here asks the backend
to change.

---

## 1. The one thing to understand first

**A product is what a customer talks about. A variant is what they buy.**

`WANAS Hoodie` is a product. `WANAS Hoodie in Olive, size M` is a variant, and
the variant is what carries the price and the stock. **`variant_id` is the only
thing that can go into a cart.** There is no "product plus chosen options" path
anywhere in this system.

The catalog is **18 products / 208 variants / 114 currently in stock.**

Everything else follows from that split, including the three rules in section 5
that will bite a frontend built on the usual assumptions.

---

## 2. Data shapes

### Product

```json
{
  "product_id": "wanas-hoodie",
  "name": "WANAS Hoodie",
  "category": "Hoodies & Sweatshirts",
  "department": "unisex",
  "style": ["oversized", "pullover"],
  "collection": "WINTER COLLECTION",
  "colors": ["Black", "Grey", "Olive"],
  "sizes": ["S", "M", "L", "XL"],
  "lengths": [],
  "price_from": 650,
  "price_to": 700,
  "original_price_to": 900,
  "on_sale": true,
  "any_in_stock": true,
  "has_size_chart": true,
  "description": "Oversized pullover hoodie. Model wears M (70kg, 178cm).",
  "images": ["…"],
  "color_images": { "Black": ["…"], "Grey": ["…"], "Olive": ["…"] }
}
```

- `category` is one of six: `T-Shirts`, `Hoodies & Sweatshirts`, `Polo Shirts`,
  `Joggers & Sweatpants`, `Jackets`, `Tops`.
- `department` is `unisex` or `women`.
- `style` is a filter axis, 13 values.
- **`collection` is nullable and 8 of 18 products are null.** It's a shelf, not a
  route. Never build navigation that requires it.
- `colors` / `sizes` / `lengths` are **the full run, including sold-out ones** —
  for rendering the selector. They say nothing about availability.
- **`color_images` is empty for 5 products** (both Tops, the three Cairokee
  items) whose photos were never split by colour. Fall back to `images`.

### Variant

```json
{
  "variant_id": "wanas-hoodie-m-olive",
  "product_id": "wanas-hoodie",
  "size": "M",
  "color": "Olive",
  "length": null,
  "price": 650,
  "original_price": 900,
  "on_sale": true,
  "status": "sold_out"
}
```

- `status` is `in_stock` / `low_stock` / `sold_out`. **The exact count is not
  exposed to customers** — `status` is what you render.
- `color` is present on every variant. `length` is `Long` / `Short` on the Worker
  Jacket only, and null everywhere else.

### Size chart

12 charts cover all 18 products. Each returns per-size measurement rows, labels
in English and Arabic, and a chart image.

Two of them are irregular and a generic renderer will get them wrong:

- **`worker-jacket`** carries `length_specific: true` and has separate
  short-sleeve and long-sleeve measurements on each row. Which one to show
  depends on the length the customer picked.
- **`wns-tops`** has **S/M/L only, no XL.** Don't assume four rows.

Every chart carries a note that the numbers are **garment measurements laid
flat, not body measurements**. Show it. A customer who reads a 31 cm waist as a
body measurement concludes the trousers are for a child.

---

## 3. Endpoints

Read endpoints are public. Everything under `/api/cart` and `/api/orders` needs
a session (cookie for guests, token if you build accounts).

### Catalog

| | |
|---|---|
| `GET /api/categories` | The six categories with counts, plus the `style`, `colour`, `size` and `department` facets and the two collections |
| `GET /api/products` | Query: `category`, `style`, `color`, `size`, `department`, `collection`, `q`, `sort`, `page`. Returns products + `count` |
| `GET /api/products/{product_id}` | One product **with its full variant list** — this is what the product page needs |
| `GET /api/products/{product_id}/size-chart` | The chart, or `{"has_chart": false}` |

`q` searches name, category, style and variant colours together, so "olive
hoodie" works even though olive isn't in any product name.

### Cart

Server-held, keyed by session. **Not** client-side state you sync later.

| | |
|---|---|
| `GET /api/cart` | Lines, item count, subtotal |
| `POST /api/cart/items` | `{variant_id, quantity}`. Quantity 1–10 |
| `PATCH /api/cart/items/{line_id}` | `{quantity}`. 0 removes the line |
| `DELETE /api/cart/items/{line_id}` | |

A cart line:

```json
{ "line_id": 4, "variant_id": "wanas-hoodie-s-olive",
  "product_name": "WANAS Hoodie", "size": "S", "color": "Olive", "length": null,
  "quantity": 1, "unit_price": 650, "unit_original_price": 900, "line_total": 650,
  "image": "…", "status": "in_stock" }
```

**The cart carries `status` per line.** Something can sell out while it sits
there; surface it before checkout rather than at it.

### Shipping

| | |
|---|---|
| `GET /api/governorates` | 27 entries: `key` (English, stable), `label_ar`, `fee`, `available` |
| `GET /api/shipping-fee?governorate=Cairo` | `{ "fee": 60 }` or `{"error": "no_rate_set"}` |

**A governorate with no fee set cannot be ordered to.** Handle it in the picker,
not at submit.

### Checkout

```
POST /api/orders
{ "customer_name": "…", "governorate": "Cairo", "address": "…",
  "contact_phone": "…", "email": null, "payment_method": "cash_on_delivery" }
```

Success:

```json
{ "order_id": "WNS-1042", "status": "Confirmed",
  "items": [ { "product_name": "WANAS Hoodie", "size": "S", "color": "Olive",
               "quantity": 1, "unit_price": 650, "unit_original_price": 900 } ],
  "subtotal": 650, "discount_amount": 0, "shipping_fee": 60, "total": 710 }
```

Refusals — **every one of these can happen at submit even if the UI looked fine**:

| Error | Meaning |
|---|---|
| `cart_empty` | |
| `missing_fields` + `fields` | Something required is blank. Governorate is never inferred from the address |
| `no_rate_set` | That governorate has no shipping fee |
| `items_out_of_stock` + `items` | The live re-check failed. **Nothing was written** — the cart is intact and the customer can adjust |
| `client_blocked` | |

### Orders

| | |
|---|---|
| `GET /api/orders/{order_id}` | With `contact_phone` as a check for guests |
| `GET /api/orders` | Account only |

Status: `Confirmed` → `Packed` → `Shipped` → `Delivered`, or `Cancelled`.
**After `Shipped` nothing can be changed** — don't render controls that will be
refused.

---

## 4. Identity

- **Guest checkout is normal.** Most orders will be guest orders.
- Phone is required. **Email is optional** — the WhatsApp side never collects
  one, so plenty of customers have none.
- **If the phone or email matches an existing customer, the API returns a
  pending link rather than merging.** The customer has to confirm ("Is this
  you? We'll remember your address") before the records are joined. The masked
  name comes back for the prompt; the full record does not, because an
  unconfirmed match is possibly a stranger.

```json
{ "pending_link": { "matched_on": "phone", "masked_name": "M… A…" } }
```

Confirm with `POST /api/clients/link { "confirmed": true }`.

Silently linking is the same operation as showing one customer another
customer's address. That's why it's a step.

---

## 5. The rules that will bite you

### Availability is per combination, not per axis

The single biggest difference from a normal catalog. `colors` and `sizes` on a
product are the full run; **which crossings exist is only in the variant list.**

The Cairokee T-shirt has XL in Black and **not** in Brown. Compute selectable
sizes from the variants matching the chosen colour, and vice versa — never from
the product-level lists.

**Show sold-out combinations disabled, don't hide them.** A customer needs to see
that their size exists and is out.

Some products are nearly gone: the WANAS Zip-Hoodie has **1 of 12** combinations
buyable, and the Cairokee Hoodie has **0 of 8**. Both states need a real design.

### Price can change with colour

Three products cost more in one colour. The WANAS Hoodie is 650 in black and
olive, **700 in grey**.

- Listing cards: use `price_from` / `price_to`. Where they're equal, one number.
- Product page: **the price must react to the selected variant.**
- **Never show `original_price` as the top of a range.** It's the highest
  *pre-discount* price (900 on that hoodie) — a strike-through number, not one
  anyone pays.

Quoting 650 and charging 700 at the door in cash is the worst failure this store
has available.

### The server is the authority on stock

Client-side hints are a convenience. Every cart action and the final order are
re-checked server-side, and `items_out_of_stock` at submit is a normal outcome,
not an edge case — minutes pass between adding to a cart and confirming.

Design the recovery path: which line failed, what's still available, cart intact.

### Money

Four separate numbers, and the frontend shouldn't recompute them:
`subtotal`, `discount_amount`, `shipping_fee`, `total`. All EGP, all plain
numbers.

**Shipping isn't known until a governorate is picked**, so the cart shows a
subtotal and says shipping comes at checkout. The final total appears before the
confirm button, never after.

---

## 6. Out of scope for the first version

- **Discount codes** — the field and the API exist in the design but aren't
  built. Leave room, don't build it.
- **Card and wallet payment** — cash on delivery only. There's no gateway.
- **Image search** — the "send us a photo" flow is WhatsApp-only for now.
