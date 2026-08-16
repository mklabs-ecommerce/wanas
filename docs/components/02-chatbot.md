# 02 — Chatbot (conversation layer)

## Purpose

The shared conversational logic behind WhatsApp, Facebook DM, Instagram DM, and TikTok DM. One layer, not four separate bots — each channel plugs its incoming/outgoing messages into the same engine, so a rule fixed once fixes it everywhere.

**It is an LLM tool-use agent, not a keyword classifier.** The model handles understanding and phrasing; it has no access to the catalog, the cart, or the order book except through a fixed set of tools. Every fact in every reply comes from a tool call.

## Why an LLM, and why this shape

The earlier design classified intent by keyword and replied from templates. Against real Egyptian customer messages that breaks in three predictable places:

- **Dialect and franco-Arabic** — "بكام دي", "في مقاس L؟", "3ayez el hoodie". None contain the keywords a matcher looks for.
- **Fuzzy product references** — "الهودي الزيتي" has to resolve to the `WANAS Hoodie` in Olive rather than the `WANAS Zip-Hoodie` in Olive, off a phrase that names neither exactly.
- **Compound messages** — "عايز الـ black hoodie مقاس L و الـ olive polo مقاس M" is one message, two products, two sizes.

Each of these turns into "I didn't understand" → menu → human. The handoff rate is the cost, and it lands entirely on staff.

**But an LLM that answers from its own knowledge is worse than a keyword matcher,** because it fails silently and confidently: it will invent a product, quote a price that was true last month, or promise a size that sold out. A wrong "yes, we have it in M" costs more than an unnecessary handoff.

So the split is deliberate and load-bearing:

| The model does | The model never does |
|---|---|
| Understands what the customer wants, in any dialect or spelling | Knows a price, a size, or what's in stock |
| Decides which tool to call, with which arguments | Decides whether an order can be placed |
| Turns tool output into Egyptian Arabic | Writes to a database |
| Asks the next question in the flow | Confirms an order without the tool succeeding |

Everything in the right-hand column is enforced by the tools, not by asking the model nicely in the prompt. A prompt instruction is a preference; a tool that refuses is a guarantee.

## Architecture

Four parts, each ignorant of the others' internals. This separation is what makes the provider swappable and the logic testable.

| Layer | Responsibility | Must not know |
|---|---|---|
| **Agent** | The tool loop, the system prompt, the flow | Which provider is active; how state is stored |
| **Provider** | Translates between a neutral message format and one vendor's API | Anything about orders, carts, or products |
| **Tools** | All data access and every business rule | That an LLM exists, or how to talk to a customer |
| **Session** | Conversation history per channel identity | Anything about the conversation's meaning |

**The neutral message format** is the contract between agent and provider. Three shapes: a user message, an assistant message (text and/or tool calls), and a tool-results message. Each provider translates to and from this; the agent only ever speaks it. Adding a provider means writing one class — no other file changes.

**Tools return facts, never sentences.** A tool returns `{"error": "out_of_stock", "alternatives": [...]}`, not "sorry, that's sold out." The model does the phrasing. This keeps the tools testable without an LLM in the loop, and keeps tone changes out of business logic.

**A reply can carry attachments as well as text.** Some tool results include an image path — a size chart, and later a product photo. The agent collects those from the turn's tool results and hands them to the channel adapter alongside the reply text; the model writes the words, it never emits a URL or a file path into the message body. The adapter decides how to deliver them (WhatsApp sends a real image message, see `04-whatsapp-channel.md`). Keeping attachments as structured output rather than links in text is what stops the bot pasting a local file path to a customer.

## The tool loop

1. Load conversation history for this channel identity.
2. Append the incoming message.
3. Call the model with the system prompt, the history, and the tool schemas.
4. No tool calls → that's the reply; save and send.
5. Tool calls → execute all of them, append the results, go back to 3.
6. **Cap the loop.** Around 8 turns. A model stuck calling tools without ever replying must hit a ceiling and return a graceful message, not spin.

Executing all tool calls from one turn together (rather than one per round trip) matters for compound messages — "the black hoodie in L and the olive polo in M" should resolve in one pass.

## The tools

**Seventeen.** The summary below is orientation; **`15-tool-contracts.md` has the exact arguments and return shapes and is what you build against.** Each returns a JSON object, never prose.

**Ordering:**

| Tool | Returns | Notes |
|---|---|---|
| `get_categories` | The six categories with counts, plus the `style` and `department` facets and the two optional collections | The opening move; grounds the model in what exists. Collections come last and marked optional — 8 of 18 products have none |
| `get_products` | Products by category, style, department or free-text search | Searches `name`, `category`, `style` and variant `color` together — that's what makes "الهودي الزيتي" resolvable now that olive is a colour rather than a product name |
| `get_variants` | Every variant of one product with its `variant_id`, price, and stock | **Must be called before adding to cart** — the `variant_id` cannot be guessed |
| `add_to_cart` | The updated cart, or a refusal with alternatives | Refuses out-of-stock variants and returns what *is* in stock |
| `view_cart` | Lines and total | |
| `remove_from_cart` | The updated cart | By line, by variant, or clear all |
| `get_size_chart` | The measurements table for a product, plus the chart image to send — or an explicit "no chart for this product" | See below; sizing questions are common and the wrong answer causes a return |
| `get_shipping_fee` | The fee for a governorate, or "no rate set" | Called while collecting the address, so the summary shows a real total |
| `confirm_order` | Order number and summary, or a refusal | The only tool that writes an order |

**After the order — required for Phase 1's definition of done:**

| Tool | Returns | Notes |
|---|---|---|
| `get_my_orders` | The customer's open orders with status and items | Called before any change. Its output is what lets the model ask "which order?" when there's more than one |
| `modify_order_quantity` | The updated order **with a recalculated total**, or a refusal | Sets a line to an absolute quantity. Increases decrement stock like a new order; `shipping_fee` is never re-quoted. The model reads the new total back — a silently changed amount becomes an argument at the door |
| `cancel_order` | Confirmation, or a refusal | Same status rule; releases stock |
| `request_item_swap` | Acknowledgement that staff will review | **Never applies the swap itself.** Swaps always go to a human (see below), so this tool queues and notifies rather than changing the order |
| `submit_feedback` | Confirmation the rating was saved | Records the stars and optional text after delivery — without it the bot asks a question it can't record the answer to |

**Escalation and identity:**

| Tool | Returns | Notes |
|---|---|---|
| `request_human` | Confirmation the conversation is paused | The single way a conversation leaves the bot. Sets a pause flag that **only a staff action clears** — not a timer, and not the model deciding things look fine again |
| `get_my_profile` | What we know about this customer, or `known: false` | Read before asking for shipping details, so a returning customer confirms an address instead of retyping it. Also surfaces a pending phone/email match |
| `link_client` | The linked client, or a fresh record | Called after the customer answers "Is this you?". Nothing is linked without it |

**The status rule lives in these tools, not in the prompt.** `modify_order_quantity` and `cancel_order` check the order's status themselves and refuse a shipped order regardless of how the request was phrased. A prompt rule about shipped orders would be a suggestion the model could talk itself out of; a tool refusal is not.

**`request_item_swap` returns an acknowledgement, not a result,** because the answer isn't known yet — staff have to check stock for the replacement and decide. The model must not imply the swap is done.

**Out-of-stock returns alternatives with the refusal.** Without them the model has to guess a replacement or make another call; with them the recovery is one message: "الـ M خلص، بس عندي L و XL." The alternatives are also the only thing the model may offer — anything not in that list would be invented.

### Size charts

"مقاس L مقاسه كام؟" is one of the most common questions on a clothing DM, and getting it wrong doesn't produce a confused customer — it produces a return.

`get_size_chart(product_id)` returns one of two shapes, and the difference is the whole point:

- **A chart exists** → the measurement rows per size, and the **path to the chart image**. The model states the numbers for the size asked about *and* the runtime sends the image alongside the reply — the picture answers follow-up questions the text won't.
- **No chart exists** → `{"has_chart": false}` and nothing else. The model says the chart isn't published for that item and offers a person, or the model's height/weight from the product description as a rough reference. **It does not estimate measurements.**

**All 18 products currently have a chart** (12 charts — see `08-product-database.md`), but the "no chart" branch still has to be built. New products arrive before their charts do, and that's exactly the moment the model would be tempted to reach for a neighbouring one.

**A chart covers a cut, not a category.** `T-Shirts` and `Polo Shirts` each span two charts, and `Hoodies & Sweatshirts` spans five. Never infer a chart from `category` or `style` — quote only what `get_size_chart` returned for that exact product.

**Measurements are garment-flat, not body measurements.** A waist of 31 cm on a size S is the waistband laid flat, roughly half the way around. A customer who reads it as a body measurement will conclude the trousers are for a child, so the reply has to say which it is. The misreading is predictable, so this belongs in the system prompt as a standing instruction, not left to the model's judgement.

**Two charts are conditional, and the model has to notice:**

- **`worker-jacket`** carries `length_specific: true` and separate short-sleeve and long-sleeve *columns* on each size row. The right number depends on the `length` the customer picked, so the bot asks or confirms which before quoting a sleeve measurement.
- **`wns-tops`** has **no XL** — the two Tops are only made in S/M/L, and the chart matches. If the model pattern-matches "charts have four sizes" it will invent one.

**Identifiers are opaque to the model.** It gets a `variant_id` from `get_variants` and passes it back. It never constructs one. This is why `get_variants` before `add_to_cart` is a hard sequence and not a suggestion.

**The customer's identity is injected by the runtime, not passed by the model.** Cart and order tools take the channel identity from the request context. If the model supplied it, a confused or manipulated model could read or modify another customer's cart.

## Guardrails

Enforced in the tools, then restated in the prompt — both, not either:

1. **No invented products, prices, sizes, or measurements.** Every product fact comes from a tool result in the current conversation. Sizing is the sharpest case: if `get_size_chart` says there's no chart, the model says there's no chart. It never estimates a number, and it never reuses another product's chart.
2. **`get_variants` before `add_to_cart`.** Enforced by `variant_id` being unguessable.
3. **Stock is re-checked inside `confirm_order`,** not trusted from earlier in the conversation. Minutes may have passed.
4. **`confirm_order` requires name, governorate, full address, and contact phone.** The tool rejects the call if any is missing — it does not fill in blanks, and it will not guess a governorate from the address text, because that's what the shipping fee is priced on.
   - **The summary shown before confirming must include the shipping fee and the real total.** A customer who agrees to a number and is handed a larger one at the door is the single most expensive mistake this flow can make on cash on delivery.
5. **Cash on delivery only** this phase. Any other payment method is declined clearly.
6. **The model never says an order is placed before the tool returns success.** A confirmation the backend didn't record is the worst failure available.

## The system prompt

Owns four things, and nothing else:

- **Persona and tone** — Egyptian Arabic, short WhatsApp-length messages, one question at a time, a line per product, prices in EGP. Not paragraphs.
- **The flow** — greet → categories → products → variants → cart → "anything else?" → name → address → phone → summary → explicit confirmation → `confirm_order`.
- **The hard rules** above, in plain language.
- **Data quirks the model would otherwise get wrong** — that **every product has a colour choice**, so colour is asked for alongside size on all of them; that the Worker Jacket adds a third choice (`Long` / `Short`); and that most of the catalog is on sale, so both prices are worth mentioning.

Collecting contact details **one at a time** is a real requirement, not politeness. Asking for name, address, and phone in one message reliably produces one blob of text that has to be re-parsed and re-confirmed.

## Customer identification

Each platform hands over a different ID: WhatsApp a phone number, Facebook/Instagram a page-scoped user ID, TikTok its own. None is a `client_id`. A mapping table connects them:

| channel | external_id | client_id |
|---|---|---|

- First message from a new `external_id` → creates (or, when ordering, prompts for) a Client DB entry.
- Returning `external_id` → looked up instantly, no re-asking.
- **Cross-channel identity is never auto-merged.** If the same person messages from WhatsApp today and Instagram next week, those stay separate by default. If the phone number given at checkout matches an existing client, the bot *offers* to link them ("Is this you? We'll remember your address") — a customer-confirmed link, not an automatic guess. `07-client-database.md` applies the same rule to guest checkout.

## Language

Customers write mixed Arabic with embedded English — product names above all, but also sizes and terms they'd never recognise translated. The design matches that rather than fighting it:

- **Replies are Egyptian Arabic with product names, sizes, and type words kept in Latin script**, inline. This is instructed in the system prompt, not implemented as translation.
- **Product names are stored once, in their natural form** (`WANAS Hoodie`) and carry no colour — colour is a variant field and used as-is in every reply. Never translated or transliterated — a customer who searches or asks again will use the English name.
- **Input needs no language detection.** The model reads franco-Arabic, dialect, and English natively. The character-range check the old design needed is gone.

## Conversation state

History lives in the **database, keyed by channel + external_id** — not in process memory. Two reasons, both operational: the server can restart without customers losing their conversation, and more than one instance can run behind a load balancer without customers landing on an instance that doesn't know them.

- **History is capped** (~40 messages). Older messages are dropped.
- **Trimming must start at a user message.** Cutting between a tool call and its result leaves the history malformed and providers reject the whole request. This is the single easiest thing to get wrong here.
- **Sessions expire** after a few hours of silence and start fresh.
- **The cart is separate from the history** and lives in the database, so it survives a session reset. Nothing is written to the Order DB until `confirm_order` succeeds — an abandoned conversation reserves nothing.

## Provider

Gemini Flash to start: fast, cheap, and adequate for a tool-calling agent whose output is short. **The provider is a configuration value, not an assumption** — expect to re-evaluate on cost. That is exactly why the provider layer exists, and why nothing above it may import a vendor SDK.

Provider differences that have to be absorbed *inside* that layer, not leaked upward:

- Tool schemas are described differently by each vendor, and some reject an empty parameter object outright.
- Some models return an opaque signature with each tool call that must be echoed back in history, or the next request is refused.
- Reasoning/thinking settings differ by model generation and are worth disabling for this workload — the replies are short and latency matters more than deliberation.

## Order flow

1. Recognise the customer, or start fresh.
2. Customer picks products — `get_variants` before anything is added, so only real variants are offered.
3. **Confirm shipping details every time, never silently.** A known customer isn't asked to retype their address, but is always shown it for a yes/no ("Ship to [saved address]?") — they may have moved. A "no" updates it for this order and becomes the new default unless they say it's one-off. Contact details follow the same confirm-don't-skip rule.
4. Payment method — cash on delivery this phase.
5. Final live stock re-check inside `confirm_order`.
6. Summary shown for review.
7. Customer confirms explicitly.
8. Backend commits the atomic transaction (see `01-backend-platform.md`).
9. Staff alert in the dashboard inbox, and the customer's WhatsApp confirmation. No email in Phase 1 — none is collected.
10. On `Delivered`: WhatsApp feedback request.

## Modification & cancellation

The same tool-use loop, using `get_my_orders`, `modify_order_quantity`, `cancel_order` and `request_item_swap` above.

- If there's more than one active order, the bot asks which one before doing anything.
- Quantity changes and cancellations apply automatically before `Shipped`; item swaps always route to staff; nothing changes after `Shipped`.
- Every automatic change is confirmed back to the customer and logged for staff.

**For an already-shipped order the bot distinguishes two situations:**

| Customer wants… | Response |
|---|---|
| More of the same, a different size, or an extra item | Offer a **new order** with that item pre-filled — a blocked change becomes a sale |
| Something wrong with what arrived (wrong item, damaged) | A **complaint, not a change request** — straight to human support, never "just order again" |

This has to be resolved before responding, not after. Offering a fresh order to someone reporting a damaged item lands badly, so it belongs in the prompt's rules and in how the modification tools are described.

## Failure handling

The customer never sees a stack trace, and staff always see the real error.

- **Rate limit / quota** → "الضغط عالي شوية، ابعتلي تاني بعد دقيقة."
- **Auth or configuration error** → a generic apology to the customer, and a loud log entry, because this is a deployment problem and no customer message will fix it.
- **A tool raises** → return an error dict rather than crashing the conversation; the model apologises and continues.
- **Empty model reply** → log the finish reason. Usually a token limit or a content filter, and invisible otherwise.
- **A debug flag** that surfaces real errors in the reply — invaluable while building, and it must default to off.

## Human handoff

The model calls `request_human(reason, summary)`. That sets a pause flag on the channel identity: while it's set the runtime stops calling the model for that conversation entirely — incoming messages are stored and shown to staff, not answered. **Only a staff action in the dashboard clears it.** Not a timer, and not the model deciding the conversation looks normal again; a human is handling it now.

**Phase 1:** an incoming image also goes straight here with the photo attached, since image recognition (`14-image-recognition.md`) isn't built yet. The **runtime** raises this before the model sees anything — the model is never handed an image, so it can't be the thing that classifies one.

## Interactions with other components

- **Backend services** (Order, Inventory) — the tools call these, exactly as the website does.
- **Client DB** — read for recognition, written for new/updated customers.
- **Product DB** — read for search and live variant availability.
- **Order DB** — read for "which order do you mean," written via the Order service.
- **Admin dashboard** — receives human-handoff, item-swap, and custom-request entries.

## Edge cases worth knowing about

- **The model calls a tool with a plausible but wrong `variant_id`** — the tool returns `variant_not_found` rather than guessing at the nearest match. Silently correcting an identifier would ship the wrong size.
- **A customer changes their mind mid-flow** ("لأ خليها M") — handled naturally by the model, which is the main thing a keyword matcher couldn't do.
- **Multiple open orders during a modification request** — the bot asks which one before acting.
- **Starting a new order while a draft is open** — the old draft is overwritten; nothing was committed, so there's nothing to reconcile.
- **A customer asks for sizing on a product with no chart** — doesn't happen today, will happen the first time a product ships ahead of its chart. The answer is "we don't have a published chart for this one," optionally with the model's height/weight from the description as a rough reference, and an offer to put them through to a person. Never a number the model produced.
- **A customer asks about a Worker Jacket sleeve without saying Long or Short** — the chart has both; ask which rather than quoting one.
- **A customer asks about XL in a Top** — there is no XL in that cut. Say so; don't extrapolate the L row.
- **A customer sends their own measurements and asks which size to take** — the bot can compare them against a chart when one exists, but should frame it as guidance rather than a guarantee, and must not do it at all without a chart.
- **A model reply that contradicts a tool result** — the tool result wins. Worth logging when the reply names a product or price that appears in no tool result in the conversation; it's the earliest signal that the prompt or the tool descriptions have drifted.
- **TikTok's non-clickable links** — handled at the Payment service level, but the bot has to explain the extra step rather than sending something that looks tappable.
