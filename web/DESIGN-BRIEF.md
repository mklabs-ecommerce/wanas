# Wanas Gallery — web store design brief

A brief for designing the customer-facing online store. Everything here is real:
the catalog numbers, the colours, the awkward cases. Design against them rather
than against a tidy imaginary shop, because the tidy version falls apart on
contact with this inventory.

**This is a separate phase.** The WhatsApp bot, the backend and the admin
dashboard are already being built. The store reuses that same backend and does
not change it.

---

## 1. The shop

**Wanas Gallery** — an Egyptian streetwear label. Oversized, boxy cuts. Hoodies,
tees, polos, sweatpants, one worker jacket, two women's tops. Prices run
**200–800 EGP**. Cash on delivery. Delivery across Egypt, priced per governorate.

**Who's buying:** Egyptians in their late teens to late twenties, arriving from
Instagram, on a phone, on a mobile connection that isn't always good. They speak
Arabic and they read product names in English — "الهودي الزيتي" is a normal
sentence and both halves matter. They are used to ordering through DMs, so the
store has to feel *at least* as easy as sending a message, or they'll go back to
messaging.

**Design mobile-first and mean it.** Desktop is the secondary layout, not the
canvas you design on and then squeeze.

---

## 2. Language and direction

- **Arabic is the primary language, right-to-left.** Not an English site with a
  translation toggle bolted on — the RTL layout is the default composition.
- **Product names stay in Latin script inside Arabic text.** `WANAS Hoodie`,
  `Ringer Tee`, sizes `S / M / L / XL`. They are never translated or
  transliterated. Every Arabic screen has English words sitting inside it, so
  the type has to handle mixed-direction lines gracefully — that's a layout
  requirement, not a detail.
- An English/LTR mode exists as a mirror, but Arabic is what you design first.

**Type:** pick an Arabic typeface with a real weight range and a Latin companion
that sits at a matching optical size. Mixed-script lines look broken when the
Latin is visually smaller or lighter than the Arabic around it.

---

## 3. Brand

Taken from the shop's own size charts, which are the only existing brand
artefacts:

| | Hex | Use |
|---|---|---|
| Navy | `#1F2535` | The base. Deep, slightly blue-grey, not black |
| Cream | `#EADBBE` | Headings, accents, the warm counterweight |
| White | `#FFFFFF` | Product photography sits on white |

The existing charts are **cream on navy**, with product line-art in cream or on a
white field. That contrast is the identity: dark ground, warm type, product
photography as the only bright element.

**Photography is the design.** There are **177 product photos** and they're good
— models, real garments, natural light. The interface should get out of their
way. Large images, generous negative space, restrained chrome. Nothing should
compete with a photo.

Tone: confident and quiet. Not loud e-commerce. No countdown timers, no "only 2
left!" banners, no urgency theatre — the stock signals in this brief are honest
ones and should read as helpful, not as pressure.

---

## 4. The catalog, and the three things that will break a naive design

**18 products. 208 variants. 114 currently buyable.**

| Category | Products |
|---|---|
| Hoodies & Sweatshirts | 6 |
| T-Shirts | 5 |
| Polo Shirts | 2 |
| Joggers & Sweatpants | 2 |
| Tops | 2 |
| Jackets | 1 |

Filters: **style** (13 values — `oversized`, `boxy-fit`, `zip-through`,
`quarter-zip`, `crewneck`, `wide-leg`, `knitted`, `graphic`, `ringer`,
`pullover`, `lightweight`, `worker`, `fitted`), **colour** (12), **size** (S/M/L/XL),
**department** (unisex / women).

Two optional collections exist — `WINTER COLLECTION` and `CAIROKEE MERCH` — but
**8 of 18 products belong to neither.** Collections are a shelf on the homepage,
never a navigation path, or half the shop becomes unreachable.

### 4.1 Availability is per combination, not per axis

This is the single most important interaction in the whole store.

A product has colours *and* sizes, and **not every crossing exists**. The Cairokee
T-shirt has XL in Black but **not** in Brown. Picking a colour has to re-evaluate
which sizes are still selectable, and vice versa.

Design the selector so that:

- **Sold-out combinations are visible but clearly disabled** — never hidden.
  Someone looking for their size needs to see that it exists and is out, not
  conclude the shop doesn't make it.
- The disabled state has to survive being looked at on a phone in daylight.
  Grey-on-grey at 30% opacity is not enough; a strike-through or a diagonal rule
  reads better.
- Changing colour must not silently reset a valid size choice, and must not leave
  an invalid one selected.

**Reality check for the design:** several products are down to almost nothing.
The WANAS Zip-Hoodie has **1 of 12** combinations buyable. The Cairokee Hoodie
has **0 of 8** — completely sold out. Both need to look intentional, not broken.
Design the "1 left of 12" state and the "entirely gone" state explicitly.

### 4.2 Price can change with colour

Three products cost more in one colour. The **WANAS Hoodie** is 650 EGP in black
and olive, **700 in grey**.

So on a listing card, a single price is a lie. Show a range — "من ٦٥٠ ج" / "from
650 EGP" — and let the product page resolve to the exact price once a colour is
picked. **The price display has to react to variant selection.** A customer who
sees 650 and is charged 700 at the door, in cash, is the most expensive mistake
this store can make.

### 4.3 Most of the catalog is on sale

**14 of 18 products** have discounted variants, and some discounts are steep —
the WANAS Quarter-Zip is 500 down from 900, the Sweatpant 650 down from 1000.

When almost everything is discounted, a red "SALE" badge on almost every card
stops meaning anything and starts looking like a clearance bin. Find a treatment
that shows the saving with dignity: the old price present but quiet, the new
price primary. Reserve any loud treatment for something genuinely exceptional.

---

## 5. Screens to design

### Home
Entry point from Instagram, so it has to answer "what is this shop" in one
screen. Hero, the two collections as shelves, category entry points, a strip of
new or featured pieces. Not a long marketing page.

### Category listing
The main browsing surface. Product grid, filters (style, colour, size,
department), sort. **Design the filter UI for a phone first** — a bottom sheet or
full-screen panel, not a desktop sidebar that gets crushed.

Each card: photo, name, price (or range), sale treatment, and a stock signal
where relevant.

### Product detail
The most important screen. It needs:

- **The photo set**, with colour-specific images where they exist. Picking olive
  should show the olive one. (For 5 products the photos aren't split by colour —
  design a graceful fallback rather than a broken swatch.)
- **Colour swatches** — 12 colours exist, including `Vintage Green`,
  `Camel Brown`, `Light Brown`. Swatches need real colour chips *and* names;
  camel brown and light brown are not distinguishable from a dot alone.
- **Size selector**, with availability reacting to the chosen colour.
- **The Worker Jacket has a third axis** — `Long` or `Short` sleeve. One product
  only, but the layout has to accommodate three selectors without looking like a
  configurator.
- **Price**, resolving as selections are made.
- **Size chart** — 12 charts exist as images plus structured measurements. Design
  it as a panel or modal, and include the line that these are **garment
  measurements laid flat, not body measurements**. Without it customers read a
  31 cm waist as a body measurement and conclude the trousers are for a child.
- Short description, usually including the model's height and the size they wear.
- Stock signal for the selected combination.

### Cart
Line items with their variant spelled out — "WANAS Hoodie — Olive, M". Quantity
control, running subtotal. Shipping isn't known yet at this stage because it
depends on governorate, so the cart shows a subtotal and says so.

### Checkout
Keep it to one page if possible, or short steps with visible progress.

- Contact details, **governorate picked from a list of 27** (it sets the shipping
  fee, so it can't be a free-text field), address.
- Guest or account.
- **Cash on delivery** is the payment method. Design for it being the normal
  choice, not a fallback.
- **A summary showing subtotal, shipping, and total, before confirming.** The
  customer is agreeing to hand over a specific amount of cash at their door;
  that number must be unmissable.
- A "is this you?" confirmation step exists when the phone number matches an
  existing customer — design a small, low-friction moment for it.

### Order confirmation and tracking
Order number, what was bought, the total, what happens next. Status runs
Confirmed → Packed → Shipped → Delivered.

### Account (optional)
Saved details, order history, past feedback. Deliberately minimal — most
customers won't register.

---

## 6. Deliverables

- Mobile screens for everything in section 5, RTL/Arabic.
- Desktop for home, listing, and product detail.
- **The variant selector in every state**: available, sold out, selected,
  colour-changes-availability, product entirely sold out, three-axis (Worker
  Jacket).
- A component sheet: buttons, inputs, product card, price + sale, stock badges,
  filter chips, size-chart panel.
- Colour and type tokens, defined for dark ground.

---

## 7. Please don't

- Don't hide sold-out variants. Showing them is a deliberate decision.
- Don't design a single fixed price into the product card — three products break it.
- Don't make collections a nav item; 8 products have none.
- Don't put a badge on every product because most are on sale. Nothing means
  anything then.
- Don't design an LTR site and mirror it at the end. RTL first.
- Don't add urgency mechanics. The stock signals here are honest, and dressing
  them up as scarcity marketing makes the honest ones untrustworthy too.
