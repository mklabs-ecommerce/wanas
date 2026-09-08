# The chatbot, internally

How a customer's message becomes a reply: the agent loop, the prompt, the
tools, what the model remembers, and every place the system refuses to guess.

Brand-agnostic. Nothing here is specific to the shop currently deployed on it
— see [REUSING_FOR_NEW_BRANDS.md](REUSING_FOR_NEW_BRANDS.md) for the parts
that are.

Related: [ARCHITECTURE.md](ARCHITECTURE.md) for the layering,
[DATA_FLOW.md](DATA_FLOW.md) for the end-to-end paths,
[`AGENTS.md`](../AGENTS.md) for the business rules the tools enforce.

---

## 1. The shape of a turn

```
inbound message
      │
      ▼
channel adapter          verify signature → claim message id → download media
(assistant/channels/*)   → record_inbound → hand to dispatcher → return 200
      │
      ▼
dispatcher               debounce ~6s per conversation, merge fragments,
(assistant/dispatcher.py) run on a worker thread, one turn per conversation
      │
      ▼
runtime                  pause check → voice → transcript → photo → reading
(assistant/runtime.py)   → open one Shopify snapshot for the whole turn
      │
      ▼
agent                    ┌── call the model with prompt + context + 19 tools
(assistant/agent.py)     │      │
                         │      ├─ no tool calls → this is the reply
                         │      └─ tool calls → run them all → append results ─┐
                         │                                                     │
                         └─────────────── loop, capped at TOOL_LOOP_CAP (8) ◄──┘
      │
      ▼
guards                   truncation · dangling promise · path leak ·
                         tool-call leak · markdown · bidi (at send)
      │
      ▼
adapter sends            text, then one image per attachment, then interactive
```

**The webhook never answers.** It verifies, claims, downloads, records, and
returns 200. The turn runs afterwards on a worker thread. The endpoint is
`async` and the turn is synchronous, so doing the work inline would block
every other conversation in the process — and a webhook that times out is one
the platform eventually switches off.

---

## 2. Debouncing: fragments are one message

People type in pieces. Three lines inside five seconds — the product, then the
colour, then the size — is one request. Answered one at a time it costs three
model calls and reads like three different people replying.

`assistant/dispatcher.py` collects everything for one conversation for
`MESSAGE_DEBOUNCE_SECONDS` (default 6), pushing the deadline out on each new
fragment, then runs **one** turn with the fragments newline-joined. A
per-conversation lock means one conversation is never answered by two turns at
once.

Scope, stated plainly: this is in-process, sized for one instance. Two
instances debounce independently — correct, just less effective. A restart
loses what is buffered, which is why the idempotency claim is taken at ingest
and not here. `shutdown()` flushes the buffer rather than dropping it, so a
deploy does not abandon mid-sentence customers.

---

## 3. The prompt

`assistant/prompt.py` — one document, built per surface:

- `SYSTEM_PROMPT` is the whole thing: voice, language policy, what the shop
  sells, the refusal rules, the tool-use rules, formatting.
- `build_system_prompt(extra, channel=...)` returns it untouched for the
  default channel. For a second channel it swaps one **surface line** and
  appends a channel paragraph — and raises if that line has drifted out of the
  prompt, rather than silently telling an Instagram customer they are on
  WhatsApp.
- `extra` is appended per turn and is **never stored**. Two things use it: the
  resume instruction after a handoff is taken back
  (`assistant/recovery.py`), and the "that was cut off, write it shorter"
  nudge. The model must read the instruction, not its own broken output.

The prompt is exempt from the line-length lint (`pyproject.toml`) on purpose:
it is prose in a `.py` file, and re-wrapping it changes what the model reads.

**A prompt instruction is a preference; a tool refusal is a guarantee.**
Anything that must never happen is enforced in a tool or a post-processing
guard, not asked for in the prompt.

---

## 4. Tools

19 tools, in four modules, registered by a `@tool` decorator into `REGISTRY`
(`assistant/tools/base.py`). The schema sent to the model is generated from
the registry, so the model can never be told about a tool that does not exist.

| Tool | Module | Required | Optional |
|---|---|---|---|
| `get_categories` | catalog | — | — |
| `get_products` | catalog | — | category, style, department, collection, query |
| `get_variants` | catalog | — | product_id, color, more_images |
| `get_size_chart` | catalog | — | product_id |
| `get_shipping_fee` | catalog | governorate | — |
| `ask_governorate` | catalog | — | region |
| `add_to_cart` | cart | variant_id | quantity |
| `view_cart` | cart | — | — |
| `remove_from_cart` | cart | — | line_id, variant_id, clear_all |
| `confirm_order` | order | customer_name, governorate, address, contact_phone | email |
| `get_my_orders` | order | — | include_closed |
| `modify_order_quantity` | order | order_id, variant_id, quantity | — |
| `cancel_order` | order | order_id | — |
| `request_item_swap` | order | order_id, from_variant_id | to_variant_id, note |
| `get_return_terms` | order | — | order_id |
| `submit_feedback` | order | order_id, rating | text |
| `request_human` | support | reason, summary | — |
| `get_my_profile` | support | — | — |
| `link_client` | support | confirmed | — |

### How tools get selected

The model chooses; the system constrains what a choice can mean.

- **All of a turn's tool calls run together**, not one per round trip. "The
  black hoodie in L and the olive polo in M" is one message with two products
  and resolves in one pass.
- **Arguments are validated before the handler runs**
  (`validate_arguments`). A missing required argument returns the tool's own
  `missing_error` code, not an exception.
- **Repeated identical calls inside one turn are served from a per-turn
  cache** (`_cached_result`), so a loop that re-asks the same question does
  not re-read the shelf.
- **The loop is capped** at `TOOL_LOOP_CAP` (8). Exhausting it returns a
  deterministic "say that in one sentence" reply rather than an unbounded
  spend.

### Implicit product resolution

`get_variants` and `get_size_chart` may be called with **no** `product_id`,
meaning "the one we are already talking about" (`_IMPLICIT_PRODUCT_TOOLS`).
"What sizes?" is a follow-up, and refusing it made the bot re-ask a question
the customer had already answered.

`last_product` reads the **newest** product reference deterministically out of
the stored history. Three things move it, most recent winning:

1. a successful `get_variants` / `get_size_chart` call;
2. a `get_products` search that matched exactly **one** product — two or more
   is browsing, and "which of these" is an ambiguity worth keeping;
3. the customer replying to a **photo**, which carries the product it was of.

A *failed* lookup never counts. An omitted id resolves; a **wrong** one is
still refused. `no_product_in_context` is the one case where asking is right.

### A refusal carries the way back

A product id lives only in a tool call's arguments, and context compaction
drops those — so a long conversation can leave the model unable to see an id
it used earlier, and it will reconstruct one. Refusing the guess is right;
refusing with an empty hand is what made the bot invent a product's colours
after `product_not_found`.

`catalog_tools._not_found` attaches `product_in_conversation`, read
deterministically from history. It is a fact to call the tool again with,
**never** a substitution — swapping a made-up id for a real one behind the
model's back is the wrong-product answer, one level deeper.

---

## 5. Memory: what is stored vs. what is sent

Two different questions that used to have one answer, and conflating them is
what made the bot forget a product discussed ten minutes earlier.

| | Setting | Default | What it bounds |
|---|---|---|---|
| Live slice | `HISTORY_CAP` | 150 | what `session.load()` returns and a turn continues from |
| Verbatim window | `MODEL_CONTEXT_MESSAGES` | 24 | the last N messages sent **exactly as stored** |
| Recall | `MODEL_CONTEXT_RECALL` | 60 | older messages, compacted |
| Archive | `SESSION_ARCHIVE_CAP` | 2000 | the whole stored transcript |
| Conversation end | `SESSION_EXPIRY_HOURS` | 6 | idle before `context_start` moves forward |

`assistant/context.py` builds the provider's view:

- the last `MODEL_CONTEXT_MESSAGES` go through **verbatim** — tool calls,
  results, reasoning signatures, all of it;
- everything before that is **compacted**: what the customer and the bot said
  is kept, the tool machinery under it is dropped.

**Nothing here summarises.** No second model call, no paraphrase, no judgement
about which product mattered. Compaction removes *whole messages only*, so
every sentence the model reads is the exact sentence that was said. A
compressed sentence that quietly loses "black" is worse than a shorter
history.

Two invariants the file exists to hold:

- **the verbatim window must never open on a `tool_results`** whose call was
  compacted away — a result with no call is the most reliable way to have a
  whole request refused. `start` walks *forward* past them.
- **alignment may only ever keep more, never less.** The recalled block reads
  better opening on something the customer said, but a conversation the *shop*
  started (a status push, a nudge) has no user message in front of its first
  line at all. `_opening` snaps **backwards**, so alignment can never discard
  the one line saying what the conversation was about.

### The transcript is append-only

A conversation *ending* — six hours idle, a staff reset, the cap — moves
`SessionRow.context_start` forward and **deletes nothing**. `session.load()`
reads the live slice; `session.transcript()` reads everything. The dashboard
must use the latter, because a read must never be what ends a conversation.
Only `session.purge()` deletes, and nothing calls it.

### A turn is not the only writer

`agent.run_turn` reads history once and holds it for the whole tool loop — but
a tool can write to the same row while it runs. `confirm_order` does, through
the notification service. So the end-of-turn `save` passes `merge_since` (the
length it started from) and folds those messages back in. Without it, the
confirmation the customer had on their phone was overwritten and the dashboard
showed a shorter conversation than the customer's own.

For the same reason `confirm_order` **ends the turn**: the confirmation is
composed and sent by the notification service, so a model reply after it is a
second confirmation for one order.

---

## 6. Reply guards

Everything the model produces passes through deterministic checks before it
reaches a customer. Each one exists because the failure it catches actually
shipped.

| Guard | Catches | Behaviour |
|---|---|---|
| `_is_truncated` | `finish_reason` in `length`/`max_tokens` | regenerate twice asking for shorter, then `TRUNCATED_FALLBACK` |
| `_is_dangling_promise` | "I'll check and get back to you" with no tool having run | retry twice, then a fallback that *asks a question* instead |
| `strip_paths` | a local file path echoed out of a tool result | removed |
| `strip_tool_leaks` | a tool call written out as prose text | removed (pattern built from the live registry) |
| `strip_markdown` | `**bold**`, headings; `-`/`*` list markers | bold/headings removed, list markers rewritten to `•` |

**Why truncation matters more than it looks.** A reply that hit the token
ceiling reads as ordinary text right up to where it stops. `finish_reason` is
the *only* signal, and a turn produces exactly one reply — so a fragment is
the whole answer the customer gets. The same guard covers a truncated
*tool-call* hop, which is a lookup the model never finished specifying.

**A truncated transcript is worse than none.** Half a voice note does not read
as broken, it reads as a *shorter message*, and the whole turn is built on it.
So a provider's `transcribe()` returns `""` on a ceiling hit, which is the
documented "hand it to a person" signal.

**Bidi is applied at the send boundary only** (`common/bidi.py`), in each
client's `send_text`. The transcript, the dashboard, and every search over
stored messages still see plain text.

---

## 7. Media

See [MEDIA.md](MEDIA.md) for the detail. In short:

- **Voice notes** are transcribed by the provider and folded into the turn as
  ordinary text. Every note in a batch, not just the first. Nothing
  transcribable → a handoff plus a short acknowledgement.
- **Photos** are read by the provider into a structured `ImageReading`
  (product_id, confidence, description, is_garment). Below
  `IMAGE_MATCH_CONFIDENCE` the reading is used only to ask a better question.
  **A vision reading is a hint about which tool to call, never a fact.**
  Nothing readable → a handoff plus an acknowledgement.
- Multiple photos each get a numbered note, so "the second one" in the next
  message resolves to something.

`supports_audio` / `supports_vision` are declared by the provider so the
runtime can fall back to a person *before* spending a call finding out.

---

## 8. Quoting: "reply to this message"

Every stored message carries the platform ids it was sent or received as
(`mids`). Outbound ids come from the send's response body.
`assistant/quoting.py` resolves an inbound `context.id` against the **whole**
transcript, archive included, and folds the original sentence into the turn.

An unresolvable id is left unannotated **on purpose**: a reply to something
older than the transcript is still an ordinary message, and inventing which
one it was is the wrong-message answer.

**A quoted photo resolves to the photo, not the message it rode in on.** One
reply is often several sends — the words, then one picture per colourway —
sharing a single stored message and differing only by id. Resolving to the
message handed the model back all four colours the customer had just pointed
away from. Each photo's id is stored against *what it showed*
(`mid_labels`), which is what lets a reply to a photo move the conversation to
that product.

---

## 9. Escalation and pausing

`request_human` raises a `HANDOFF` queue item and **pauses** the conversation:
the message is still stored, the model is not called at all, and only a staff
action clears the flag. Nobody is answering that customer until a person opens
the dashboard — which is why every handoff raises an owner email.

**Taking a handoff back.** A handoff raised over a *message* rather than a
person's decision ("I could not follow that", "I cannot open a sticker") is
undone by the customer's next typed message, if it arrives soon enough
(`assistant/recovery.py`). Every condition is explicit; none of them is a
guess about whether things "look normal again". A handoff a *person* caused is
never auto-resumed.

---

## 10. Error handling

| Failure | Response |
|---|---|
| Provider rate limit / 5xx | one retry after 30s, then `RATE_LIMITED` / `GENERIC_FAILURE` |
| Provider auth error | **not** retried — it will fail identically in 30s |
| Tool raises | caught, returned to the model as a tool error, the turn continues |
| Tool loop exhausted | `LOOP_EXHAUSTED` — asks for the request in one sentence |
| Turn crashes | webhook claim released so the platform's retry is processed |
| Shopify unreachable, browsing | fall back to local catalog numbers, log once |
| Shopify unreachable, ordering | **refuse** — `store_unavailable` |
| Photo/voice unreadable | handoff + acknowledgement |
| Conversation paused | message stored, no model call, warning logged |

The customer never sees a stack trace; staff always see the real error.
`CHATBOT_DEBUG=1` surfaces raw provider errors in the reply and must stay off
in production — it is logged loudly at boot if it is on.

---

## 11. Business logic that lives outside the model

These are enforced by `domain/services/`, not asked for in the prompt:

- **Ordering reads live stock**, bypassing the per-turn snapshot. Minutes can
  pass between "add it" and "yes" — long enough for the storefront to sell the
  last one.
- **The order is created on the store first**, then written locally. A store
  order with no local row is visible and fixable; a local row the store never
  heard of is stock that quietly sells twice. If the local write fails, the
  remote order is **cancelled** and the failure named in the log.
- **Inventory is never decremented twice.** `orderCreate` decrements the store;
  the local write is bookkeeping only (`record_sold`, not `decrement`).
- **Order status only moves forward, one stage at a time** (`advance_status`).
- **"Back in stock" describes a verified transition** — observed at or below
  zero then, above zero now — never just a positive number today.
- **An automatic client link is never silent.** Phone numbers get reused,
  shared and mistyped, so a match raises a *pending* link the customer must
  confirm; the name shown before confirmation is masked.
- **Deliverability is decided inside the transaction that writes the
  message.** Outside the platform's 24-hour service window with no approved
  template, the line is stored `delivered=False` and a staff alert is raised.
  A stored message is not proof it arrived.
