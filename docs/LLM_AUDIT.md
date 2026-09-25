# What the model decides, and what code should

An audit of every place the language model is trusted with a fact, a number, a
decision or a claim that deterministic code could own instead. The rule it
applies is the one `AGENTS.md` already states -- *a prompt instruction is a
preference; a tool that refuses is a guarantee* -- and asks, line by line,
where the codebase still settles for the preference.

## How it was done

The model is reached from exactly five places (`grep` for `generate(`,
`transcribe(`, `inspect_image(`, `read_size_chart(`, `classify_comment(`):

| Call | Where | What it decides |
| --- | --- | --- |
| `generate` | `assistant/agent.py` | the conversation: which tool, with which arguments, and every word of the reply |
| `transcribe` | `assistant/media.py` | a voice note's text |
| `inspect_image` | `assistant/media.py` | which catalog product a customer's photo resembles |
| `read_size_chart` | `dashboard/shopify_api.py` | a size-chart picture's numbers (staff review every cell before saving) |
| `classify_comment` | `assistant/channels/instagram.py` | a public comment's category (the reply itself is a fixed line) |

Everything the model reads comes from `assistant/prompt.py`, the tool
descriptions and results in `assistant/tools/*.py`, the notes the runtime
writes into a turn (`runtime.py`, `media.py`, `recovery.py`, `quoting.py`,
`agent.py`'s nudges), and the domain services behind the tools
(`domain/services/catalog.py`, `carts.py`, `orders.py`, `shipping.py`). Every
one of those files was read in full. Every other file in the repository was
checked for a route into the model and has none: the dashboard, the Shopify /
WhatsApp / Instagram / mail integrations, the schedulers and the scripts move
data the model never sees, or show staff what it already said.

`scripts/quality_gate.py` deserves its own line. It already holds eighteen
deterministic checks on model replies -- an invented payment method, a
misspelled shop name, a mangled product name, a whole product line denied
without a lookup -- each written after a real failure. **Every one of them
runs offline, against a benchmark, and never against a reply a customer is
about to receive.**

## Ranked findings

Risk is how likely the model is to get it wrong; impact is what it costs when
it does. Money a courier collects at the door ranks first.

| # | Risk | Impact | Where | What the model does today | The deterministic version |
| --- | --- | --- | --- | --- | --- |
| 1 | High | Critical | `domain/services/carts.py::cart_payload` | Quotes cart prices from `variant.price`, the seeded wanas.db column -- while `place_order` charges Shopify's live price. The pre-confirmation summary can promise a number the order will not charge. | Price the cart through the same live overlay `get_variants` and `place_order` use. |
| 2 | High | Critical | `get_shipping_fee`, prompt "# لما ييجي وقت الأوردر" | Adds the cart subtotal and the shipping fee itself to state "the real total" before `confirm_order`. | `get_shipping_fee` returns the checkout preview -- subtotal, fee and total -- computed in code. |
| 3 | High | High | every reply | States prices and measurements with nothing checking them against a tool result. «كل رقم ... لازم تكون جاية من نتيجة أداة» is a preference. | A reply guard: every money amount and every centimetre figure must appear in this conversation's tool results (or be one of the shop's own published constants); otherwise the reply is regenerated, then replaced. |
| 4 | High | High | `catalog.get_variants` payload | Aggregates 10-24 variant rows into "which sizes are available in which colour", by hand, and orders them S→XL because the prompt says so. | The payload carries `available` / `sold_out` per colour, sizes already in order, and one price per colour. |
| 5 | Medium | High | `confirm_order.contact_phone` | Extracts the courier's phone number from the conversation. Only blankness is checked: a transposed digit, a number the customer never typed, or a landline all go to Shopify. | Validated as an Egyptian mobile, and refused unless it is a number the customer typed, the saved profile number, or the WhatsApp number they are writing from. |
| 6 | High | High | `assistant/prompt.py`, `comment_faq.py` | Quotes shipping (110), the exchange surcharge (20), the exchange window (24h) and delivery time (4 days) as literals in the prompt -- a second copy of numbers whose source of truth is the rate table and `orders.py`. The prompt's format examples also carry invented prices (Knitted Polo 499, Ringer 500, "الشحن للقاهرة — 60 جنيه", which contradicts the flat 110). | The prompt is built from the rate table and the constants in code; examples use no real-looking prices. |
| 7 | Medium | High | every reply | Can say "ضفتهولك" when `add_to_cart` refused, or "الأوردر اتسجل" when `confirm_order` never succeeded. | A claim guard, the same shape as `order_change_claims`: an action claimed without a successful tool call this turn is regenerated, then replaced. |
| 8 | Medium | High | every reply | Can deny a whole line ("مفيش قسم حريمي") without looking, or answer «فيه قمصان؟» with «أيوه» -- gate rules 9 and 18, offline only. | Both run live: a denial with no catalog lookup this turn, or a yes to a `garment_not_sold`, is regenerated. |
| 9 | Medium | Medium | every reply | Reconstructs product names from memory -- «Lightwelson Sweatpant», six times in one conversation (gate rule 8, offline only). | Corrected live, word for word, to the catalog's spelling. |
| 10 | Medium | Medium | `order_payload` / `order_summary` | Receives both `order_id` (internal, `WNS-12`) and `reference` (Shopify's `#1040`) and must remember to say only the second. | An internal id in a reply is replaced with that order's reference before it is sent. |
| 11 | Low | Medium | every reply | Can offer InstaPay, a card or a payment link (gate rule 6, offline only). | Checked live and regenerated. |
| 12 | Medium | Medium | prompt "# المقاسات" | Must remember to say measurements are garment-flat "every time". | Appended in code to any reply quoting centimetres without it. |
| 13 | Medium | Medium | `request_human` | Writes the sentence the customer reads after a handoff, and can promise a callback time nobody gave. | The turn ends on a fixed line the shop wrote, the same way `confirm_order` does. |
| 14 | Low | Medium | `media.catalog_shortlist` | Is offered **archived** products to match a customer's photo against, and told "the closest product we have is X". | Archived products are not offered. |
| 15 | Low | Low | every reply | Misspells the shop's name (gate rule 7, offline only). | Corrected live. |
| 16 | Low | Low | every reply | Can send the previous reply again (gate rule 10), or profess not to know a sleeve length the tool returned (gate rule 12). | Checked live and regenerated. |
| 17 | Low | Low | every reply | Emoji rules (one at most, none beside a price or an apology) are prompt-only. | Enforced by the reply sanitiser. |

## What stays with the model, and why

These need language understanding, and nothing deterministic can replace it:

* **Reading the customer** -- intent, «الاتنين» / «ده» / «التاني», a
  message with two readings, whether to ask or act. The tools refuse the
  consequences of a misreading (a guessed `variant_id`, an unknown governorate,
  an `unclear` handoff before any question) but cannot do the reading.
* **Extracting what the customer typed** -- a name, an address, a quantity.
  The governorate (`shipping.detect`) and now the phone are checked against
  what was actually written; a free-text address cannot be without the model.
* **Phrasing** -- the Arabic itself. What goes around it is increasingly
  code: `common/bidi.py`, the sanitisers, the claim guards.
* **Transcription and photo reading.** Already bounded: a photo may only name a
  product from the real catalog, below a confidence threshold nothing is
  named, and every failure goes to a person.
* **Comment categories.** The public words are fixed lines
  (`comment_faq.py`, `comment_replies.py`), and the FAQ answers are matched
  before the model is asked anything.
* **Recommending a size to a body.** The charts are garment measurements, not
  body measurements, and there is no data to compute a fit from. The guard in
  finding 3 makes sure every number the model *quotes* is the chart's own.

## Status

Every finding above is implemented, each committed on its own or with the
findings it shares a change with:

| Findings | Change |
| --- | --- |
| 1, 2 | the cart priced through the live overlay; `get_shipping_fee` returns `checkout` with the total computed |
| 3, 12 | `assistant/reply_facts.py`: ungrounded amounts and measurements regenerated; the garment-flat note appended |
| 4 | `get_variants` carries `by_color` |
| 5 | `orders.egyptian_mobile` in `place_order`; `confirm_order` refuses a number the customer never gave |
| 6 | `domain/services/shop_facts.py`; the prompt is a template filled from the rate table |
| 7 | `assistant/action_claims.py` |
| 8, 9, 10, 11, 15, 16, 17 | `assistant/reply_rules.py`, shared with the quality gate |
| 13, 14 | `request_human` ends the turn on `HANDOFF_CLOSINGS`; archived products are out of photo matching |
