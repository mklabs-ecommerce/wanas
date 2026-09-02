# Changelog

## Unreleased — A negative comment stops promising a DM that never comes

`negative` is answered in public and deliberately never DMed — chasing a
critic into their inbox is how a bad comment becomes a screenshot. But the
public lines ended in "تحت أمرك في الدايركت", inviting exactly the DM the
routing refuses to open. Published under a live post, that is an invitation
nobody honours: a customer who accepted it wrote into a thread the shop had
not opened and no alert pointed at, and a hater who accepted it got the
private argument the no-DM rule exists to avoid. The bank now acknowledges
that the comment was read and stops there.

That makes the alert the only thing standing behind a negative comment, so it
is now raised at `high`, like `customer_complaint`. A complaint at least has
an open DM thread behind it; here a queue item nobody works is a comment
nobody ever read.

`test_no_negative_variant_mentions_the_dm` holds the rule across the whole
bank rather than the one variant a given `crc32` happens to pick.

## Unreleased — The bot answers about the shop, and stops when a person takes over

Two bugs, both of them the bot talking when it should not have been.

- **The bot answered questions that have nothing to do with the shop.** Asked
  for the capital of France it said Paris. Every *number* it says was already
  required to come from a tool, but that rule was written about prices and
  sizes — nothing had ever told it that its own general knowledge is off-limits
  too, and a model asked a question it knows the answer to will answer it.
  `assistant/prompt.py` now has a scope section: it is a shop assistant, not a
  general one, and an off-topic question gets one friendly line back to the
  shop. The section is written to leave no partial-credit option, because the
  likely failure is not refusing — it is saying "Paris" and *then* offering a
  hoodie. Two things it deliberately does not do: it is not a handoff (trivia
  in the staff queue helps nobody, so `# التحويل لموظف` now says so out loud),
  and it does not catch greetings — a bot that answers "إزيك" with a redirect
  reads as broken.

  This is the one rule in the prompt with no tool behind it, which is a
  property of the problem: answering a trivia question is free text, not a tool
  call, so there is nothing to refuse. The module docstring says so rather than
  leaving it looking like a violated invariant.

- **A staff takeover ended itself.** `POST .../reply` un-paused the
  conversation as a side effect of sending, so the bot was live again the
  instant a staff member hit send — and the customer's next message, the answer
  to a sentence a *person* had just written, came back from the model. Two
  voices in one thread, mid-handover.

  Replying no longer releases. `/takeover` pauses, `/release` ("رجّع البوت")
  un-pauses, and nothing else touches the flag; staff send as many messages as
  the conversation needs and hand it back when they are done. That reverses an
  earlier decision, so the reasoning for both halves is written into
  `dashboard/web.py::reply` — auto-release was there to stop a forgotten pause
  from silencing a number forever, and that risk is now covered where it
  belongs: the inbox and thread header both mark a paused conversation, every
  dropped inbound is logged with how long it has been waiting, and
  `python manage.py release-conversation` is the escape hatch. The composer note and the
  send toast no longer promise a handover that is not happening.

## Unreleased — Every comment gets an answer, in its own words

- **Twelve categories where there were six, and every one of them answers
  somebody.** `important` used to absorb every real question a shopper can
  ask — price, stock, size, colour, fabric, "where is my order" — and hand
  them all one identical public line and one identical DM. They are now
  `price`, `availability`, `size`, `variant`, `product_info` and
  `order_status`, routed the same but *worded* differently, which is the
  point. `tag_friend` splits from the old `neither`, and `other` replaces it
  as a catch-all that answers rather than a bucket that does nothing.
- **`negative` is no longer silence.** A bad word under a live post was
  classified, alerted on, and never answered, for months. It now gets one
  short, calm, un-defensive public line — written for the hundred people
  reading it, not for the one who wrote it — and still no DM, because
  chasing a critic into their inbox is how a bad comment becomes a
  screenshot. `spam` stays the one category with no customer-visible answer:
  replying to a scam bot republishes it to everyone scrolling past.
- **Routing is a table, not a branch.** `_ACTIONS` in
  `assistant/channels/instagram.py` says what each category may do — public
  line, DM, alert, priority — so "does every category answer somebody?" is a
  property a test reads rather than a missing `else` nobody notices. That
  missing `else` is exactly how `negative` shipped mute.
- **Categories own banks of wording, not one line each.** New module
  `assistant/comment_replies.py`: 4–8 hand-written variants per category plus
  a per-category DM opener, so two people asking the same question stop
  getting byte-identical text with the quoted comment swapped in. Selection is
  `crc32(comment_id) % len(bank)` — **deterministic, never random**: Meta
  redelivers any webhook it does not get a clean 200 for, and a retry has to
  reproduce the same sentence rather than post a second, differently worded
  reply under one comment. Every string is still fixed and hand-written; the
  rule that the public surface never shows a model-chosen sentence is
  unchanged.
- **Prices in public: allowed by Meta, still answered in DM, and no longer
  promised falsely.** Meta's private-reply rules govern *initiating* a DM (one
  per comment, inside 7 days) — they say nothing about quoting a price in a
  comment reply, and the fixed product-independent answers (shipping,
  delivery, payment) already are public. A per-product price stays in DM for
  accuracy, not policy: "بكام؟" under a post showing several pieces does not
  say which one, and a wrong number published under a post outlives the sale.
  What changed is the honesty — no public line claims the price has already
  been sent, which one of them did while the DM behind it was a bare opener.
- `INSTAGRAM_COMMENTS_DM_ENABLED` (default on) mirrors
  `INSTAGRAM_PUBLIC_REPLY_ENABLED` for the private half: answer in public,
  cold-DM nobody.
- A classifier outage is still the one silence, and still raises
  `classifier_unavailable`. An outage is not a category, and guessing at one
  would publish a line no model chose.

## Unreleased — Answering a comment where it was asked

- **Three questions are now answered in public, and end there.** "الشحن
  بكام؟", "التوصيل بياخد قد إيه؟", "بتاخدوا كاش؟" have one answer, identical
  for every customer and every product. They used to classify as `important`
  and cost a DM each, when one sentence under the post answers them
  completely — and answers them for everyone else scrolling past too.
  `assistant/comment_faq.py` is a **lookup, not a classifier**: no model call
  is reachable from it, on the same principle the fixed `PUBLIC_ACKS` and
  `domain/services/search_terms.py` follow — when the answer is a rule, it
  lives below the model, where it is a rule. Matching runs over the catalog
  search's own Arabic/franco normaliser and needs two things present, never
  one: a subject and the question being asked about it, so `الشحن بكام` is a
  flat rate, `الشحن كام يوم` is a delivery time, and `الهودي ده بكام؟` is a
  product question that still reaches the model.
  - Its own budget, `INSTAGRAM_FAQ_RATE_LIMIT` (default 5/hour per
    commenter), counted apart from the 3/hour DM cap: that cap exists to stop
    a flood of DMs, and an FAQ reply sends no DM and costs no model call — but
    it is visible under a post, so it is not unlimited either. Over it, the
    comment drops quietly; a chatty commenter is not a flood.
  - With `INSTAGRAM_PUBLIC_REPLY_ENABLED=0` an FAQ match falls through to the
    DM handoff. That flag means "do not speak in public", never "ignore the
    customer".

- **A complaint is no longer filed under "negative".** They read alike to a
  sentiment model and deserve opposite treatment: a hater ignored is fine, a
  paying customer ignored *in public* is the worst outcome available on this
  surface. `complaint` gets a fixed public line that **admits nothing** —
  nobody has looked at the order yet — plus the DM handoff and a
  `customer_complaint` alert staff read above `negative_comment`.

- **Spam gets an alert and nothing else.** `InstagramClient.hide_comment`
  still ships with a test and no caller, deliberately: hiding is invisible to
  the shop, so a misclassified real customer would vanish with no trace anyone
  could follow. The owner hides by hand, from the alert.

- **A classifier outage is now silence, not a burst.** `_classify` used to
  fall back to `important`, which turned a provider being down into public
  replies and DMs on a live post that no model had decided on. It falls back
  to `neither` and raises a `classifier_unavailable` alert carrying the
  comment, its id, the post and the commenter — silence is the safe failure on
  a public surface, and the alert is what stops silence from meaning loss.

- **`COMMENT_CLASSIFIER_MODEL`**, empty by default, so nothing moves today.
  Not a cost setting — the call is ~250 tokens in and rounds to nothing. It is
  decoupling: without it, upgrading the chat model silently changes how a live
  public surface is classified, and one model being pulled or rate-limited
  takes chat and comments down together.

- **A follow-up under our own reply is now answered.** Every `parent_id` used
  to be dropped, which was right while the only public output was "شوف
  الدايركت" — nobody answers that. Now that a fixed answer goes out in public,
  «طب والشحن بكام؟» underneath it is both likely and reasonable, so a reply in
  a thread we replied in goes through the whole chain. A reply between two
  other people still drops. The own-account check stays first, so this cannot
  open a self-reply loop.

- **Fixed: an emoji-only comment spent one of the commenter's three hourly
  slots** and received nothing for it. The reply row was written inside the
  rate-limit block and the "says nothing" check ran after it; the check now
  runs first. The duplicate claim still happens before everything, so a
  redelivered "🔥" is not re-examined.

## Unreleased — Adding a product, with the pictures on it

- **Boot now says which products have drifted.** `RECONCILE_REPORT_ON_BOOT`
  (default on) runs the reconcile in **report mode** on every deploy and logs
  any wanas.db product Shopify no longer has. It writes nothing: deleting
  stays `scripts/shopify_reconcile_products.py --apply`, run by hand, because
  this runs unattended and a reconcile that deletes unattended is one bad
  Shopify read from an empty catalog. Three phantom products sat in production
  for weeks because nothing ever looked.

- **Taking a product off the shelf now releases everything that was waiting
  on it.** Deleting used to remove the rows and the Shopify product and stop
  there; three other places held something that only made sense while the
  variant existed, and each failed quietly:
  - a **cart** holding it failed at checkout, the most expensive place to fail;
  - a **stock-waitlist** entry meant somebody waiting forever for a
    back-in-stock message that could never come;
  - an open **`item_swap`** naming it as the replacement meant a staff member
    approving a swap into a size that is not there. The request stays open and
    the dead target is cleared off it with a note — the customer still wants a
    different size, and which one is a person's call.

  `release_variants(..., gone=)` is one path for both callers, and
  **archiving now uses it too**. That was the worse half: an archived variant
  still exists, so nothing errors — the customer simply waits forever.

- **A dashboard-made size chart goes with the last product using it.** Only a
  `size_charts` row, never one from `data/size_charts.json`, and only when no
  other product still points at it: charts are deliberately shared, so "its
  product went" and "it is unused" are different statements.

- **A size chart uploaded from the dashboard now has numbers behind it.** It
  used to be a picture and nothing else: the storefront showed the image, the
  bot could send it, and neither could answer "what is the chest on a large".
  Putting measurements there meant editing `data/size_charts.json` and running
  a script, which is not a thing anyone does at 11pm.
  - `POST /dashboard/api/shopify/size-charts/read` hands the picture to the
    vision model (`LLMProvider.read_size_chart`) and fills the grid in.
  - The reading is **never saved on its own.** It arrives as a form a staff
    member checks and corrects, because a misread measurement is a customer
    ordering the wrong size and posting it back -- the same rule
    `docs/MEDIA.md` states for customer photos: a vision reading is a hint,
    never a fact. `normalise_chart_reading` enforces the rest on this side: a
    size the product does not sell is dropped, and a cell the model could not
    read stays **blank rather than 0**, because the bot quotes these numbers.
  - `POST .../size-charts` saves the confirmed chart into the new
    `size_charts` table, points the product at it, and sets *both* Shopify
    metafields so the theme renders a real bilingual table.
  - The table **overlays** `data/size_charts.json` on a shared `chart_id`, the
    same shape as `catalog._overlay`: the twelve shipped charts still ship in
    the file and still win nothing they did not before. A file on Railway does
    not survive a deploy, which is why a dashboard chart is a row.
  - The edit drawer's free-text chart box is now the same picker plus upload
    the create form has.

- **A product deleted in Shopify Admin can now be cleaned out of wanas.db.**
  `product_import` is additive by contract, so nothing closed the other
  direction: a local product whose SKUs Shopify no longer knows gets no live
  price or stock, falls back to the seeded columns, and is still offered to
  customers. New `integrations/shopify/product_reconcile.py` +
  `scripts/shopify_reconcile_products.py`, dry-run by default. Because it is
  the one reconcile that can destroy the catalog, every judgement is made the
  cautious way:
  - `all_variant_skus()` reads `productVariants` across *every* status --
    archived and draft products still exist, and only a deleted one counts --
    and raises on a failed page rather than returning what it managed.
  - an empty live read is refused outright, and `--force` does not lift that;
  - more than half the catalog looking gone is refused too, and *that* is what
    `--force` is for. A shop does not lose most of its products between two
    runs; a token pointed at the wrong store looks exactly like one that did.
  - one surviving SKU keeps the whole product (partial variant drift is
    `shopify_set_skus.py`'s problem);
  - anything ever ordered is **archived, never deleted** -- the order lines
    still read;
  - it is a script, not a boot step. `product_import` runs on boot because
    the worst it can do is add a row.

- **A create that fails no longer leaves wreckage on both sides.**
  `productCreate` runs first, so every step after it -- variants, photos,
  inventory -- was happening against a product that already existed. When one
  of those failed (which is what the option-shape and SKU-shape bugs
  below did, repeatedly), Shopify kept a shell wearing nothing but its own
  "Default Title" placeholder variant at 0.00. `product_import` then did its
  job on that shell and mirrored it into wanas.db as a phantom `One Size`
  product; later the shell was deleted in Admin and the local row stayed,
  because import is additive and nothing there ever deletes. Three of those --
  `oversized-plain-t-shirt`, `-2`, `-3` -- were sitting in production, offered
  by the bot at 0 EGP. Two locks on the door now:
  - `create_product` wraps everything past `productCreate` in `_or_unmake_it`,
    which deletes the half-made product and re-raises the *original* error
    (the dashboard tells a refusal from an outage by its type, so the rollback
    must not replace it). A cleanup that fails too is logged, never raised.
  - `product_import` skips a product that is still only the placeholder --
    `admin_products.is_placeholder_only`, read off the variant's own option
    values rather than guessed from its price. It is imported the moment real
    variants land.

- **A product, or one of its sizes, can be removed.** This module used to say
  that was deliberately left to Shopify Admin; it is here now because staff
  asked, and the thing that made it risky is handled rather than avoided.
  `order_items.variant_id` is a foreign key, so:
  - nothing ever ordered from it → it really goes, from Shopify and from
    wanas.db, along with the cart lines and stock-waitlist entries that only
    pointed at it;
  - it sold before → refused, and **archive** is offered in its place:
    Shopify's `status: ARCHIVED` and a new `Product.archived`, which together
    take it off the storefront and out of the bot's search and out of
    `get_variants`, while the orders still read. An order is the record that
    money changed hands; it outranks tidying the catalog.
  - a product's **last** size is refused too. Shopify has no such thing as a
    product with no variants, and a local row with none is a product the bot
    would offer and never be able to sell -- deleting the product is the
    honest way to say that.

- **A created product was invisible three different ways**, all of them
  Shopify defaults nobody had had to think about while products were made by
  hand in Admin:
  - **Vendor** came out as the *store's* name ("My Store"), not the brand.
    `SHOPIFY_VENDOR` (default `Wanas Gallery`) is now stamped on every
    product this app creates.
  - **It was on no sales channel.** `status: ACTIVE` only means "not a
    draft": a new product has no `publishedAt`, no storefront url, and
    appears in no collection *on the site*. It is now published to the Online
    Store. That needs `read_publications`/`write_publications`; a shop whose
    token lacks them still gets its product -- and gets told, in the create
    panel, that it is not on the website yet.
  - **Category is a picker now.** Every category collection on this shop is a
    *smart* collection whose only rule is `TYPE EQUALS "<something>"`, so a
    product typed "T-shirt" instead of "T-Shirts" joins nothing at all. The
    field offers the types the shop already uses and still lets a new one be
    typed.
- Choosing a **manual** collection now actually puts the product in it. A
  smart one is labelled "(automatic)" in the picker and left to its rules --
  asking Shopify to add a manual member to one only earns a refusal.

- **Photos come off the staff member's own laptop now.** The new-product form
  had one field for an image *url*, which meant a picture had to already be
  hosted somewhere before a product could show it. Each variant row now takes
  a file, `dashboard/api/shopify/uploads` puts it on Shopify, and the create
  call attaches it.
- **A photo belongs to a colour, not to a product.** The picture on a row is
  set as that colourway's variant image in Shopify, which is the field
  `shopify_catalog.LiveVariant.image_url` reads -- so it is what the bot sends
  when a customer asks about the olive one, and what
  `catalog._overlay_images` splits the gallery by. The row's **Length** field
  made way for it; length is still stored and still shown on products that
  have it, it is just not something a new product is asked for.
- A colour with no picture of its own gets none. An unlabelled picture stands
  in only when no colour has one -- otherwise a product where Navy has a photo
  and Olive does not would show the Navy photo on the Olive variant,
  confidently and wrongly.
- **Collection is a picker, not a text box**, in both the create form and the
  edit drawer. It lists the shop's actual Shopify collections; making a new
  one is a button that takes you to the Collections screen. A typed collection
  name was a merchandising label that matched nothing. A product's current
  value leads the list even when Shopify no longer has a collection by that
  name -- an edit must not silently drop a field nobody touched.
- **A photo can be changed on a product that already exists.** Every row in
  the edit drawer takes a file, and the new picture lands on *every variant of
  that colourway*, not only the row it was picked on: a photo is of a colour,
  and leaving M/Olive on the old one while S/Olive has the new one gives
  `catalog._overlay_images` two photos for one colour. The local
  `color_images` fallback is merged, not replaced, so a colour nobody touched
  keeps what it had -- and a row belonging to another product is dropped
  before anything is uploaded, since a picture attached and then orphaned is
  one nothing in here can remove.
- **The size chart is two fields, both of them real choices**: a dropdown of
  the charts `data/size_charts.json` actually publishes, and an upload for a
  chart picture that has no measurements behind it. The upload lands in
  Shopify Files, sets the product's `custom.size_chart` metafield, and is
  kept on `Product.size_chart_image` so the bot can send it --
  `get_size_chart` answers `image_only` for it, with an empty `sizes`, so
  there is nothing there for the model to quote a number from.
  `theme/size-chart.liquid` now renders a diagram with no table beneath it.
- The staged-upload dance -- signed target, bytes straight there, then a
  mutation naming where they landed -- lives in one place,
  `integrations/shopify/files.py`, shared by the dashboard and
  `scripts/shopify_size_charts.py`.
- **Creating a product on Shopify worked in the tests and in nothing else.**
  Two input shapes had drifted and the fake shrugged at both: an option value
  is an `OptionValueCreateInput`, not a string (`Expected "L" to be a
  key-value object`), and the SKU belongs to `inventoryItem`, not to the
  variant. Both verified against the live schema by introspection, and the
  fake now asserts both shapes rather than accepting whatever it is handed.
  `product_import.py` wrote the SKU to the same wrong place when it adopted a
  product created in Shopify Admin, so that path is fixed with it.
- **A product's options are declared when it is created**, in
  `productCreate`'s own `ProductCreateInput`, not handed to a `productUpdate`
  afterwards. Bolted on after the fact they never took, so the product kept
  the default `Title` option Shopify made it with and every real variant was
  refused with "Option does not exist". `productVariantsBulkCreate` now also
  passes `REMOVE_STANDALONE_VARIANT`, which takes that placeholder variant
  away as the real ones land -- otherwise the product keeps a phantom
  "Default Title" size with no SKU, which the bot would offer for sale.
- The whole path was rehearsed against the live store once, end to end:
  photos staged, a product created, variants read back with their SKUs,
  prices, quantities and per-colour photos, the chart metafield set, the
  local mirror checked -- and the product deleted again.
- Uploads are an allowlist of jpeg/png/webp/gif, capped at 20 MB, and the
  filename is rebuilt from what is safe rather than filtered for what is not.
  SVG is refused on purpose: Shopify Files serves it on the shop's own origin.

## Unreleased — The size charts, on the product page

- **The storefront now shows the same charts the bot sends.**
  `scripts/shopify_size_charts.py` publishes `data/size_charts.json` and
  `data/size-charts/*.png` to Shopify as two product metafields --
  `custom.size_chart` (the diagram) and `custom.size_chart_data` (the
  measurements, as JSON) -- and `theme/size-chart.liquid` renders them in a
  modal behind a "دليل المقاسات · Size guide" button. Both languages show at
  once: `size_charts.json` has carried `label_ar` and `label_en` all along,
  which is the whole reason this crosses over as data rather than as a picture
  with text baked into it.
- Which product gets which chart comes from `Product.size_chart`, so nobody
  sets 18 products by hand; matching to Shopify is by variant SKU, the way
  every other reconciliation in here works. Dry run by default, idempotent,
  and a diagram somebody uploaded by hand in Shopify Admin is left alone
  unless `--replace-images` says otherwise -- theirs was a decision, ours is
  a default.
- Sizes cross over as an ordered array rather than a JSON object. Liquid
  iterates an object in whatever order it arrives in and cannot sort it back,
  so a chart listing XL before S would be unfixable in the theme.
- The panel replaces the theme's own `snippets/size-chart.liquid`, which
  showed one shop-wide page for every product. That page stays as the
  fallback for a product with no chart of its own.

## Unreleased — Stock writes, on the Shopify inventory API as it is now

- **Saving a quantity works again.** Every stock write carried
  `ignoreCompareQuantity`, which Shopify has removed from
  `InventorySetQuantitiesInput`; an unknown field fails the whole document
  before the shelf is touched, so the dashboard's "add quantity" answered
  "Could not save" every time. The same call now sends what 2026-01 onward
  actually asks for: `changeFromQuantity` (required, and the replacement for
  the long-gone `compareQuantity`) plus an `@idempotent` key.
- **The compare is satisfied, not skipped.** A staff correction still gets
  the last word — it reads what Shopify has, sets against that, and on a
  genuine race re-reads and re-applies, because the number counted on the
  shelf is still the right answer. The order path keeps its compare-and-swap:
  it is what stops two customers buying the same last shirt.
- **A refused stock write is now raised.** `shopify_set_inventory` discarded
  Shopify's `userErrors`, so a rejected save would still have reported
  success and left the numbers on screen a wish.
- **A lost race reads as a lost race.** Shopify phrases it "The
  changeFromQuantity argument no longer matches the persisted quantity" —
  wording the mismatch check did not recognise, which would have told a
  customer the store was down when somebody had simply bought the last one.
- The same three fixes land in `scripts/shopify_sync.py`. Inventory writes
  are refused outright below API 2026-01 rather than run without the compare,
  and `SHOPIFY_API_VERSION` now defaults to 2026-07 — the version the shop
  actually runs — instead of 2025-01.

## Unreleased — Delivery status, and the button that settles a COD order

- **Where the parcel is, as its own column.** Shopify's order-level
  fulfillment status stops at "fulfilled" — it says the parcel *left*, never
  that it arrived. The delivery column reads the fulfillment's own
  `displayStatus` instead (اتسلّم / في الطريق / خرج للتسليم / فشل التسليم …),
  which is the same field `fulfillments/update` has always moved a local order
  to Delivered on. It is in the table, the drawer and the CSV export.
- **"علّم كمتسلّم", beside Ship.** Written to Shopify as a fulfillment
  *event*, the same thing a courier's own integration writes, so the system
  keeps one field meaning "delivered" rather than two that can disagree. The
  local row is walked forward here too rather than left to the webhook the
  event also fires: the webhook is the right mechanism but not a guarantee
  (it needs `SHOPIFY_WEBHOOK_SECRET` and a reachable URL), and both paths go
  through `orders_service.advance_to`, so whichever arrives second finds
  nothing to do.
- **For a cash-on-delivery order, delivered is paid.** Marking it delivered
  settles it — locally and on Shopify — because Delivered is what
  `advance_status` has always keyed the payment off. Staff record one event,
  not two. An order already paid online is only moved along the delivery
  side; Shopify declines the redundant payment call and it costs a log line.
  "علّم كمدفوع" stays for an order that settled some other way, and hides
  once the order is paid.
- The stage-walk moved from `integrations/shopify/webhooks.py` into
  `domain/services/orders.py::advance_to`. The dashboard button takes exactly
  the same steps as a courier's webhook, and a second copy of that loop is how
  two paths end up disagreeing about which messages a customer gets.

## Unreleased — Payment status, and a way to change it

- **Payment status is a column and three filters.** "مدفوعة", "مستنية دفع"
  and "مسترجعة" sit beside the shipping filters, and the status has its own
  column in the table, the drawer and the CSV export — in words, not as the
  raw `PENDING`/`PAID` enum it used to print. These three are plain Shopify
  search syntax, unlike the payment *method* toggle next to them, which is
  classified server-side from the order's tags.
- **Delivery settles the order on Shopify.** Reaching Delivered already set
  the local `payment_status` — cash on delivery settles when the courier
  hands it over — but nothing told Shopify, so every delivered order went on
  reading PENDING in the admin. It is told now, after the commit and through
  `try_mark_as_paid`, which never raises: the local row is the record that the
  money was collected, and a Shopify outage must not roll back a delivery that
  happened. Delivered itself comes from Shopify's own `fulfillments/update`
  when the carrier reports the shipment delivered.
- **A cash-on-delivery order can be marked paid by hand too.** Nothing else can ever tell
  this shop that one settled: the money moves in the street, so every bot
  order sits at PENDING until a person says the courier handed it over —
  which is exactly what left the whole chatbot history looking unpaid.
  Shopify first (`orderMarkAsPaid`), the local `Order.payment_status` after,
  so a row saying "paid" that Shopify never accepted cannot exist. One way
  only, and the confirmation says so: Shopify has no "mark as unpaid" to
  offer back.

## Unreleased — A conversation is called by its customer's name

The inbox is titled by the customer's name where the shop knows it, their
phone number where it does not, and the channel's own id only when there is
nothing else — decided once, in `web.customer_labels`, and used by the inbox
list, the open thread, the dashboard's attention card and the
busiest-conversations table.

- **The open thread used to name itself from whichever list was loaded.** It
  looked its own row up in `state.inbox.items`, so a conversation opened from
  search, from the command palette, or from the attention card showed a raw id
  for a customer whose name was right there in the database. The endpoint
  answers it now, so those four surfaces cannot drift apart.
- **A phone number, not whatever id the channel handed us.** Since Meta
  started sending business-scoped ids, a WhatsApp customer using a username
  arrives as `EG.1754797805572316` and nothing else; where that person is a
  known customer their real number is on the client record, and it is what the
  inbox shows. Scoped to WhatsApp on purpose: an Instagram IGSID is all
  digits, so `is_phone_number` says yes to it, and it is not a number anybody
  can ring.
- The thread's context panel gained the name and the phone as their own rows,
  and inbox search now matches a saved phone number as well as the id — the
  two are no longer the same string for every customer.

## Unreleased — Three things the Orders screen was getting wrong

- **The order page stopped responding after opening the Ship form.** The
  overlay held one open layer, not a stack, and the Ship form is a modal on
  top of the order drawer -- so the modal's own close set `overlay.open` to
  null while the drawer was still on screen. From then on the drawer's ✕, Esc
  and the scrim all hit `if (!this.open) return` and did nothing: the page was
  stuck with no error to go on, whatever you did in the form. Layers are
  pushed and popped now, so closing the top one hands control back to the one
  underneath. Cancelling an order was the same bug -- its confirmation is a
  modal too -- as was every other confirm raised from inside a drawer.

- **An order opens again.** `fulfillmentOrders` needs a scope `read_orders`
  does not imply (`read_merchant_managed_fulfillment_orders` /
  `read_assigned_fulfillment_orders`), and this app was never granted it.
  While that field sat inside the order-detail query Shopify failed the whole
  document with one ACCESS_DENIED, so clicking any order showed a raw GraphQL
  error instead of the customer, the address and the line items — every one of
  which the token may read perfectly well. Asked separately now, and allowed
  to fail on its own: the drawer says the shipping button is locked and names
  the missing permission, and `fulfill` refuses with
  `fulfillment_scope_missing` rather than a Shopify error at the moment
  somebody clicks. Every other failure still raises — an outage and a missing
  scope are different problems, and only one of them is fixed by waiting.
- **Cash on delivery is read off the tag.** An order the bot creates goes in
  through `orderCreate` with nothing paid on it, so Shopify returns
  `paymentGatewayNames: []` for all of them and the gateway can never say how
  the customer is paying — which put the shop's entire chatbot history in
  "غير محدد". The bot writes the method as a tag, and that is now read first.
  It also settles the ones that were wrong rather than blank: collecting the
  cash and ticking "mark as paid" in the admin leaves the gateway reading
  `manual`, which classified a pocketful of cash as an online payment.
- **New/returning describes the order, not the customer as they are today.**
  It came off Shopify's lifetime `numberOfOrders`, so the moment somebody
  bought a second time their *first* order was relabelled "returning" as well
  — and the new-customer count shrank on ranges whose orders had not changed.
  It is now whether that order was its buyer's first, decided against the
  whole shop's history (`admin_orders.first_order_ids`, behind a one-minute
  cache) and keyed on the customer id or, for the orders placed before the bot
  attached customers, the normalised phone. `unknown` stays a real third
  bucket, and a walk that fails changes no labels rather than calling
  everything new.

## Unreleased — One customer list, and what each customer is actually worth

The Customers screen answered two questions (how many orders, how much spent)
with two numbers Shopify keeps, and showed them on two tabs that did not have
the same columns. It now answers four, on three tabs that do.

- **Cancelled orders are their own two columns.** "طلبات" and "أنفق" are the
  orders that still stand and what they came to; "ملغي" and "قيمة الملغي" are
  the cancelled ones. `numberOfOrders` and `amountSpent` cannot be split that
  way, so all four are folded out of the orders themselves
  (`dashboard/customer_ledger.py`) — one walk of the order list, read from the
  same `admin_orders.order_summary` dicts the Orders screen draws, so the two
  screens cannot disagree about what a customer spent. A cancelled sale
  counted as revenue is the one mistake this screen must not make.
- **Every customer says which channels they bought through** — واتساب,
  انستجرام, الموقع, or more than one where they bought more than one way.
  Read from `Order.source_channel` where there is a local row, and otherwise
  the way the shop owner reads the admin: the Channel column says Online Store
  or "Chatbot Integration", and then the tags say which conversation
  (`admin_orders._channel_hint`).
- **The bot now tags an Instagram sale `instagram`.** It tagged every order it
  placed `whatsapp`, Instagram sales included, so the admin quietly disagreed
  with the dashboard about where half the sales came from — and said it in a
  way nobody would question. Orders placed before this still carry the wrong
  tag; `Order.source_channel` has always been right and is read first.
- **The missing governorates are back.** A customer created by
  `scripts/shopify_backfill_customers.py` has no default address at all, so
  the store tab showed a dash for the very same people who had a governorate
  on the bot tab. Filled from their `Client` row where there is one, and
  otherwise from where they last had an order shipped.
- **Three tabs, one shape.** كل العملاء / عملاء البوت / عملاء الموقع are one
  list segmented server-side, not three routes returning three different
  customer dicts — switching tab no longer changes which columns exist. A
  person who bought once in a conversation and once on the site is on both
  tabs, which is the honest answer; picking one channel per person means being
  wrong about the other sale.
- **The customer drawer shows their orders the way the Orders screen does** —
  same columns, same cancelled chip, same channel, and a row opens the order
  itself. Its four KPIs are summed from the orders below it, so a drawer
  cannot disagree with its own table.

## Unreleased — Everyone who ever bought, in one list that filters

Four things, all the same complaint from different angles: the Customers
screen was not showing everyone, and the one filter that would have narrowed
it was broken.

- **The order-count filter returned nothing, always.** Shopify sends
  `numberOfOrders` as a *string* — `"1"` — and `admin_customers._summary`
  passed it straight through, so "customers with exactly one order" compared
  `"1"` to `1` and matched none of the customers with exactly one order.
  `admin_orders._customer_orders` had coerced it correctly since it was
  written; this is the same reading in the place the customers list goes
  through. The reason no test caught it: the fake shelf replaces
  `list_customers` wholesale, so the mapper never ran in the suite. It does
  now, over a real Shopify payload.
- **"كل المتجر" now means the whole store.** It was Shopify's customer list
  alone, which was short by exactly the buyers whose orders the bot placed
  before it attached a customer to them — they exist in wanas.db with a name
  and a governorate, and were missing from the list that called itself
  everyone. Merged on the phone, normalised through the order path's own
  `normalise_phone`, so `01067177128` and `+201067177128` are one person. A
  local row that matches a Shopify customer is dropped rather than summed:
  `numberOfOrders` is already that person's lifetime total across both
  channels. Each row says which side it came from, and the store list now
  always pages the whole customer list, because deduping against page one
  would list anyone on page two twice.
- **Both tabs offer the same four filters.** Order count, governorate and sort
  were on the store tab only, on the reasoning that they were Shopify-side
  facts — they are not: a bot customer's governorate is on their `Client` row
  and their order count is in `orders`. `dashboard/customer_filters.py` holds
  the one vocabulary both routers use, so the filter bar cannot change shape
  when you switch tab. The counts still mean different things and the label
  says so: lifetime orders on the store tab, bot orders on the bot tab.
- **Search runs while you type**, debounced 300ms rather than per keystroke —
  on the store tab each query pages the whole customer list. Enter still works
  and skips the wait. The re-render that follows each result rebuilds the
  toolbar, so focus and caret are put back on the fresh input; without that,
  search-as-you-type would eat the field on every result that came back.

Alongside them, the repair for the orders already placed:

- **`scripts/shopify_backfill_customers.py`** attaches a customer to the
  orders that have none. I said earlier this was impossible — that was wrong:
  `orderCreate` is the only place a customer can be *upserted*, but
  `orderCustomerSet` links an order to a customer that already exists, so the
  repair is find-or-create then link. Dry run by default like every other
  script in `scripts/`, and the dry run is the supervision: one line per order
  naming who it would link. An order with no phone and no email is skipped —
  a name is not an identity, and linking two people who share one is worse
  than the "No customer" it replaces. Idempotent, and two orders from one
  person converge on one record — which needed a fix the first real run
  found: Shopify's customer search is an *index*, and the index lags the
  write, so the second order from a person created moments earlier searched,
  found nothing, and was refused for creating a duplicate. Six of nineteen
  orders failed that way. What a run creates, it now remembers.

  Run against the live shop: 24 of 28 orders now carry a customer, from 5
  before. The four left have no phone and no email on the order at all.

## Unreleased — The order says who placed it

Every sale the bot made reached the Shopify admin with a shipping address and
no customer, which the Orders list renders as **No customer** — 23 of the last
28 orders in the shop. The name was never missing; it was only on the address,
where nothing but the packing slip reads it.

- **`orderCreate` now attaches the customer** (`shopify_orders._customer`),
  using `toUpsert`: Shopify matches on the phone and links the record the
  customer already has instead of making a second one. With neither a usable
  phone nor an email there is nothing to match on and nothing Shopify would
  create a customer from, so the block is omitted rather than sent in a form
  that could cost the sale.
- **A refusal about the customer costs the link, not the sale.** Their phone
  sitting on somebody else's record retries once without the block and logs
  why. An out-of-stock refusal is never retried — the second call is refused
  for the same reason, and the customer is owed the alternatives it raises for.
- **The name is split once**, by `_split_name`, for both the customer record
  and the address, so the Customers list and the parcel cannot disagree about
  the same person.
- **The dashboard reads the name off the address when there is no customer
  record**, which is what the existing orders get: `orderUpdate` has no
  customer field, so nothing can attach one to an order already placed —
  verified against the 2025-01 schema, not assumed. The fallback is the name
  only. `customer_order_count` stays `None` and the returning/new chip stays
  **unknown**, because an address cannot say whether this person has bought
  here before, and "new" is not a thing to guess at.

## Unreleased — Who sees the dashboard, and what the numbers are actually of

The dashboard has had one role since it existed: everyone who could log in
could do everything, and attribution was the only control. That was true while
the only two logins belonged to the people who built the shop. It is not a
staffing model. Alongside it, four screens answered questions slightly to the
side of the ones being asked — "how many orders" with no way to separate the
cash-on-delivery ones, "the customers" with no way to ask which of them come
back, a channel breakdown that called every Instagram sale a WhatsApp one.

- **Staff accounts now carry a role and a permission list**, and the owner
  manages both from a new **الفريق** section under القرار. One permission per
  dashboard section. `manage.py create-staff` grew `--role` / `--can`, and
  still defaults to `owner` so the first account on a fresh database can reach
  the screen that scopes everyone else.
- **Enforcement is on the endpoints, not in the sidebar**
  (`dashboard/guard.py::require_permission`). Hiding a nav item is a courtesy
  to whoever is reading it; the route behind a hidden button is one `fetch`
  away, so each one refuses on its own with a 403. The first test written for
  this asserts exactly that.
- **A `Staff` row with no role stored reads as an owner.** Both new columns
  are nullable, which is also what lets `domain/schema_drift.py` add them at
  boot — it reports a `NOT NULL` column with no server default rather than
  guessing at one. Reading an absent role as "staff with no permissions"
  would have shipped a deploy in which every existing login was scoped to
  nothing and nobody could open the screen that fixes it.
- **Orders and Analytics split cash-on-delivery from online**, classified once
  in `admin_orders._payment_method` from the order's `paymentGatewayNames` so
  the list and the statistics page can never disagree about what COD counted.
  An order with no gateway is `unknown`, never `online` — a draft is not
  evidence that somebody paid a card, and folding it in would inflate the
  exact number the toggle exists to separate.
- **The channel a sale came in on is read from `Order.source_channel`**, not
  from the Shopify tags. `ORDER_TAGS` hardcodes `whatsapp` on every order the
  bot places, Instagram included, and no tag can be added retroactively to the
  orders already in the shop. Analytics gets a Web / WhatsApp / Instagram /
  All toggle on the same footing as the payment one — both narrow every number
  on the page, not one chart. The money is still summed from Shopify; only the
  label comes from Postgres.
- **Orders can be filtered to returning or new customers**, from Shopify's own
  lifetime `numberOfOrders`. An order with no customer record is a third
  bucket, never counted as new.
- **Customers filters by order count (1, 2, 3, or any number typed in) and by
  governorate, and sorts by order count.** None of those three is expressible
  in Shopify's customer search or its `CustomerSortKeys`, so the moment one is
  set the route pages through the whole matching list rather than filtering
  the first fifty rows — "the customer with the most orders", computed over
  page one, is the most of that page, and nothing on screen would have said
  so. The page cap is surfaced as `truncated`. The governorate dropdown comes
  from the shop's own `ShippingRate` list, so it offers a governorate the shop
  ships to but has not sold to yet.
- **Two new things to look at**: orders per governorate, and a
  conversation-to-order rate. The second is deliberately not called "معدل
  التحويل" — the Admin API this app reads exposes no site traffic at all, so a
  real conversion rate is not derivable. It is bot orders over bot
  conversations, computed in the browser from the two payloads the page
  already loads, with a caption on screen saying so. The denominator moves
  with the channel toggle (`insights.by_channel`); under the payment toggle,
  and on the website, there is no denominator that matches, so the card is
  hidden rather than dividing by something that does not.
- **Both the Orders and the Customers list page through the whole match once
  a filter is on.** Filtering the first fifty rows would answer a different
  question with a straight face — "the COD orders" would mean "the COD ones
  among the last fifty orders", and the totals above the table would sum that
  subset while reading like store figures. The page cap surfaces as
  `truncated` and the UI says the numbers are a floor.

- **Analytics takes a specific window, not just 7/30/90.** `dashboard/ranges.py`
  parses one date window for both tabs, because two tabs of one page answering
  about different fortnights is the failure that module exists to prevent. A
  single day is `start == end`. `insights_api` needed the real work: its
  zero-filled series anchored on *today* and every filter was `>= since` with
  no upper bound, so a historical range would have drawn a flat line of zeros
  across dates that had real activity, and swept everything after the window
  into the totals. Both are fixed and both are tested. Bad input is refused
  (400), never repaired: `start` after `end` is not silently swapped.
- **A language toggle, Arabic ⇄ English.** Arabic stays the source language and
  the dictionary is keyed on it, so a missing translation falls back to Arabic
  rather than to a blank. Because that makes an omission invisible,
  `tests/test_dashboard_i18n.py` extracts every phrase the page asks for and
  fails on any that is unaccounted for — all 527 are.
  - Templates are translated through a **tagged** template, `TR`, which is what
    makes this safe: a tagged template receives its literal chunks separately
    from its `${...}` values. The chunks are developer copy; the values are
    where a customer's name arrives. No customer data can reach the dictionary.
  - `QUICK_REPLIES` is excluded by name. It is typed into the composer and sent
    to a customer over WhatsApp — the UI language is the staff member's
    preference, and an Egyptian customer should not get an English message
    because somebody's dashboard was in English.
  - `dir` flips in a head script, before first paint. That one attribute is the
    whole RTL/LTR switch, because the stylesheet was already written in logical
    properties throughout — a test now keeps it that way.
  - Strings that live on the server (the feature flags, the permission
    catalog) carry both languages in the payload, since the page's dictionary
    has never seen them.
- **The brand mark is the real logo, and the name is always "Wanas Gallery".**
  It is a name, not a label, so it is excluded from the dictionary and reads
  the same in both languages. The file is served from `/dashboard/logo.webp`
  rather than inlined, so the shop can swap it without editing HTML.

## Unreleased — The governorate in the address the customer already typed

A customer wrote "شبين الكوم المنوفية شارع 9" and the bot answered with the
region picker, as though they had not said where they live. Everything needed
to read it was already there — `shipping.resolve` knows the twenty-seven names,
their Arabic labels, and the districts people use instead of them. What was
missing was anyone looking: `ask_governorate` offered a list without reading
the message it was answering.

- **`shipping.detect` reads the governorate out of free text**, and
  `ask_governorate` calls it before sending anything. One match and the tool
  returns `step: "done"` with the key, so the turn goes straight to
  `get_shipping_fee`. No match and the region picker is sent exactly as
  before — free text is still never a source of new values, only a way of
  selecting one of the fixed twenty-seven.
- **Matching is by whole word, which is the whole defence against a false
  positive.** The substring test underneath the old last-resort match found
  "قنا" inside "القناة", so an address on شارع القناة in Ismailia resolved to
  Qena — 700km and a different fee, on a path `confirm_order` also runs
  through. Tokens are compared instead, with the definite article stripped
  from both sides, and the longest name at a position wins ("مرسى مطروح" over
  "مطروح").
- **Two governorates in one message is not a decision the bot makes.** It
  returns `step: "confirm"` with those two, and the picker it sends offers
  those two rather than all twenty-seven.
- Only the customer's own last two messages are scanned. A governorate the
  *bot* named is not the customer stating where they live, and one mentioned
  six messages ago is not this order's address.

## Earlier unreleased — The bot remembers, and knows what it is being asked about

Two complaints that read as one ("the bot gets confused") and share nothing
underneath.

- **It forgot the product.** `HISTORY_CAP` answered both "what is stored" and
  "what does the model see", and it counts messages, not exchanges. One
  customer question costs four to six — the question, an assistant message
  carrying tool calls, the `tool_results`, then the reply — so forty messages
  was about seven exchanges, and a product discussed at the start of a
  conversation was gone by the time the customer asked a follow-up about it.
  `assistant/context.py` splits the two: the last `MODEL_CONTEXT_MESSAGES`
  (24) go through verbatim, and up to `MODEL_CONTEXT_RECALL` (60) older ones
  are compacted to the sentences that were actually said, with the catalog
  dumps underneath them dropped. `HISTORY_CAP` itself goes to 150. Nothing is
  summarised: compaction only removes whole messages, so every sentence the
  model reads is the exact sentence that was said, and the stored transcript
  keeps every tool exchange for the dashboard.
- **It answered the wrong message.** A WhatsApp "reply to this" arrives as
  `context.id`, and the only thing it could ever be matched against was the
  other messages in the same debounce batch — so a reply to something the bot
  said was unresolvable outright (nothing recorded the id WhatsApp gave a
  message the shop sent; the send's response body was read for a status code
  and thrown away), and a reply to an earlier message of the customer's own
  fell outside the batch. Either way the quote was dropped and the model
  guessed from recency. Now every stored message carries the platform ids it
  was sent or received as (`mids`), outbound ones stamped on after delivery
  by `session.attach_outbound_ids`, and `assistant/quoting.py` resolves a
  quoted id against the whole transcript — archive included — folding the
  original sentence into the turn. An id it cannot find is left unannotated
  rather than guessed at. Both channels: WhatsApp and Instagram DMs.
- `Pending` also labels text fragments now, not only photos and voice notes,
  and hands on only the quotes it cannot explain itself
  (`unresolved_reply_to`), so no quote is ever described twice.

## Earlier unreleased — Every message the shop sends, in the transcript

Two bugs found by comparing a customer's WhatsApp thread against the same
conversation in the dashboard. The dashboard was showing strictly less than
had actually been said, and one of the missing messages should never have
been sent at all.

- **Nothing recorded a message the shop started.** An agent turn writes its
  reply to `sessions`; an order confirmation, a Shopify status push, the
  delivery feedback request, a back-in-stock notice and an abandoned-cart
  nudge do not go through a turn, so they went to the sender and nowhere
  else. `domain/services/notifications.py` now writes each of them through a
  registered port (`register_transcript_recorder` →
  `assistant/runtime.py::record_outbound`), inside the transaction that
  decides the message so it commits or rolls back with it. Stored as
  `by="system"`: a third voice next to the model and a staff member, rendered
  distinctly in the conversation view, and skipped by the inbox's
  `unanswered` filter — a nudge sent on a clock is not somebody answering
  the customer.
- **"Back in stock" for an item that had never been away.** `add_to_cart`
  was the one place in the bot still reading `variants.stock_qty`, the
  seeded wanas.db column, instead of the live Shopify overlay every price
  and status it quotes already comes from. `cairokee-hoodie-s-black` reads 0
  there and 1 on Shopify, so the bot refused a size the storefront was
  selling — and because a refusal is what joins the stock waitlist, the
  scheduler read Shopify half an hour later, saw a positive number, and
  announced a restock that had never happened. It reads the overlay now.
- **And the notice itself now has to prove the change.** `StockWaitlistEntry`
  gained `observed_stock`: what Shopify said the level *was* when the
  customer was turned away. `check_back_in_stock` sends only when that
  baseline is at or below zero and the level is above it now — an actual,
  verified transition rather than "the number looks healthy today". An entry
  is only created against a level Shopify confirmed, so a Shopify outage no
  longer manufactures one; rows written before the column existed are
  baselined on the first pass instead of fired on. Added additively by the
  startup schema reconciliation, no migration to run by hand.

## Unreleased — The photo of the colour that was asked for

A customer who asked for the olive hoodie was reliably shown the black one,
and the Inventory table put the same photo next to all four colourways of the
RINGER TEE. Three separate causes, all in the path between Shopify's photos
and the person looking at one.

- **The bot never knew which colour to show.** `get_variants` took a
  `product_id` and nothing else, and `_candidate_images` walked
  `color_images` in dict order — so every request got whichever colourway
  came first. The tool now takes an optional `color`, and the runtime sends
  that colourway's photo. Switching colour mid-conversation is a new
  question too: the "already shown this product" guard is now scoped to the
  colour, where it used to swallow the reply that should have carried the
  new photo.
- **Shopify's answer was only ever allowed to lead a guess.** The seeded
  `data/images/` mapping was built by matching folder names, and three of
  the RINGER TEE's four colours point at a different product's folder
  entirely — "another angle of the navy one" answered with a shirt this shop
  no longer sells. Once Shopify has a photo for *every* colourway,
  `_overlay_images` now takes it as the product's photo set rather than
  putting it in front of the seed. Short of full coverage the old additive
  rule is untouched, so nothing loses a photo it had. Five products that
  `wanas.db` never split by colour get their split from Shopify for the
  first time.
- **A colourway's lead photo is a majority vote** across its variants. HEART
  TOP's large olive has the black photo attached to it in Shopify Admin, and
  taking whichever variant sorted first meant one staff mis-click decided
  what "the olive one" looked like.
- **The dashboard was reading the product's featured image for every
  variant row.** Inventory rows and the product drawer's variant table now
  show the variant's own photo, which is also what makes a mis-attached one
  like HEART TOP's visible to staff instead of only to a customer.

All 52 product/colour combinations in the live store now resolve to their own
photo. The Shopify-unreachable fallback, the size-chart path, the one-photo
budget and `more_images` are unchanged.

## 1.2.0 — Full re-architecture: layered by responsibility, vendor-cohesive integrations

Pure structural move, no behavior change. `backend/` and `chatbot/` are
gone, replaced by eight top-level packages named for what they actually do:

- `common/` — the zero-dependency shared kernel (money, events, the shared
  Meta webhook-signature check, the file-serving path guard).
- `config/` — `settings.py`, imported by every layer below.
- `domain/` — persistence and business rules (`db.py`, `models.py`,
  `legal.py`, `seed/`, `services/`); must never import `assistant/`.
- `integrations/` — every outbound vendor client, one package per vendor
  (`shopify/`, `whatsapp/`, `instagram/`) instead of the same six Shopify
  files scattered across three old directories.
- `assistant/` — the AI agent runtime (was `chatbot/`): agent loop,
  dispatcher, session store, providers, tools, channel adapters, harness.
- `api/` — `public_media.py`, the one FastAPI route with no home in any
  other layer.
- `dashboard/` — unchanged, already a clean, self-contained package.
- `manage.py` — the ops CLI, promoted from `backend/cli.py` to a root
  entrypoint (`python manage.py <cmd>`), matching `app.py`'s own
  `uvicorn app:app` pattern.

One real bug fixed along the way: `conversation_reset.py` imported the
assistant layer directly, breaking the project's own "domain never imports
assistant" rule. Replaced with a registration callback (the same shape
`notifications.py` already used for outbound senders) instead of just
relocating the violation under a new name.

Full test suite and `ruff check .` verified clean after every step; see
`OPENCODE_PROGRESS.md` for the complete file-by-file move log.

## 1.1.0 — Instagram, a second first-class channel

Instagram DMs (`"instagram_dm"`) run on the same agent, tools, carts, orders
and staff dashboard as WhatsApp. Everything channel-specific lives in two
files — the inbound adapter (`chatbot/channels/instagram.py`) and the
outbound client (`backend/integrations/instagram_client.py`) — plugged into
machinery that was already channel-neutral.

- **Per-channel sender registry.** The Notification service's single module
  global became a registry keyed by channel; an unregistered channel falls
  back to logging and *never* to another channel's client. This is what makes
  "a staff reply to an Instagram conversation posted over WhatsApp, to an
  IGSID used as a phone number" structurally impossible rather than a bug to
  avoid. Orders now remember `(source_channel, source_external_id)` so their
  confirmations and status pushes go down the thread they were placed in.
- **Public media route.** Instagram cannot receive an upload; Meta fetches
  URLs itself. `api/public_media.py` serves catalog files (the size
  charts) behind a deterministic HMAC path token — 404 on anything wrong,
  `data/inbound` unreachable even under a correct token for another path.
- **Platform limits enforced below the model:** replies split at ~950 bytes
  of UTF-8 (Arabic is two bytes per character); quick replies capped at 13
  with 20-character titles; the 27-governorate picker degrades to a numbered
  plain-text list that lands on `shipping.resolve`'s free-text handling; no
  templates, so proactive outreach outside a live conversation becomes a
  staff alert by design.
- **Comments, shipped OFF** (`INSTAGRAM_COMMENTS_ENABLED=0`). A strict filter
  chain drops the shop's own comments first of all, then threaded replies,
  duplicates, old comments, floods and emoji-only noise; what survives gets
  one fixed public ack plus one private reply that seeds the DM session.
  One private reply per comment, ever — recorded before it is sent.
- **The 60-day token.** Instagram tokens expire silently after 60 days;
  `integrations/instagram/token.py` refreshes them from the scheduler
  within ten days of expiry, stores the result where the client reads it,
  alerts staff when it fails, and reports the expiry on `/health`.
- Dashboard: channel badges (WA/IG), a channel filter, and a visible warning
  when replying to an Instagram conversation whose 24-hour window may have
  closed. Prompt: surface-aware — WhatsApp's system prompt byte-identical,
  Instagram gets its own lines. Docs: `docs/OPERATIONS.md` has the launch
  checklist and the kill switch.

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

`domain/services/search_terms.py` folds Arabic spelling variants (`هودى`/`هودي`,
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

### A paused conversation can actually come back

`request_human` has paused a conversation and written a `StaffQueueItem`
since Phase 1. Nothing ever read that queue back or un-paused the
conversation except the dev harness's `/unpause` stand-in — a customer who
triggered a handoff **stayed stuck** until someone edited the database by
hand. This was a known, documented gap (`docs/ARCHITECTURE.md`, "Known
gap"), not an oversight, but it was still the single biggest thing standing
between this project and a real deployment.

`chatbot/dashboard/` closes it: a staff login (`Staff` already existed —
`domain/services/auth.py`, `python -m backend.cli create-staff` — with
nowhere to log in) at `/dashboard` lists conversations, waiting-on-staff ones
first, oldest wait first. Opening one shows the full history and, if it is
paused, why. Replying sends the customer a real message through the same
`OutboundSender` port the bot's own replies use, then resolves the queue item
and un-pauses the conversation in one transaction; "resolve without a reply"
covers a false alarm or a customer already handled by phone. It refuses
(409) to reply into a conversation the bot still owns — the same two-writers
race `chatbot/dispatcher.py`'s debounce lock exists to prevent.

The session is a signed cookie (`domain/services/auth.py::issue_session_token`),
not a table. With no `DASHBOARD_SESSION_SECRET` set, login refuses outright
(503) — the same call the Shopify webhook makes with no signing secret.
`DASHBOARD_ENABLED`, `DASHBOARD_SESSION_SECRET`, `DASHBOARD_SESSION_HOURS`.

Two small refactors came out of building it: `chatbot/display.py` (stored
history → bubbles a person can read) and `chatbot/media_serving.py` (the
local-file path guard) are now shared between the harness and the
dashboard, rather than living only in the harness.

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
- Test suite: 297 → 421 (405 passed, 16 skipped without live Shopify/WhatsApp credentials).
