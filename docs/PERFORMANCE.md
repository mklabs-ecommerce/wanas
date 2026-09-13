# Latency

How long the bot takes to answer, where that time goes, and what was done
about it. Every number here was measured; nothing in this document is an
estimate unless it says so in the line it appears on.

The tools are in the repository, so any of it can be re-run:

```bash
python scripts/bench_turn.py --runs 5                   # this codebase, model excluded
LLM_PROVIDER=openrouter python scripts/bench_turn.py --runs 3 --real
python scripts/quality_gate.py --check docs/perf/golden_fake.json
railway logs --service wanas --json | python scripts/latency_report.py -
railway run --service wanas -- python scripts/prod_turn_latency.py --days 30
```

---

## The baseline (2026-09-13, before any change)

### Real customers, real conversations

Read out of production's own transcript with `scripts/prod_turn_latency.py`:
every stored message carries `at` (`assistant/messages.py`), so the gap between
a customer's message and the reply that answered it *is* that turn's duration.
**213 answered turns across 25 conversations, 60 days.**

| | mean | p50 | p90 | p95 | max |
|---|---|---|---|---|---|
| **agent turn** (s) | **14.81** | 11.91 | 31.72 | 46.58 | 67.57 |
| whatsapp (n=129) | 15.06 | 12.22 | 33.87 | 46.58 | 64.92 |
| instagram_dm (n=84) | 14.41 | 11.30 | 31.00 | 39.07 | 67.57 |

Distribution: 60 turns under 5s, 37 at 5–10s, 58 at 10–20s, 46 at 20–40s,
**12 at 40s or worse.**

That measurement covers the agent turn only. It starts when the turn starts,
which is *after* the debounce window has closed, and it ends when the reply is
stored, which is *before* Meta is called. The two ends are added below.

### End to end, per average turn

| stage | cost | share | how it was measured |
|---|---|---|---|
| **model hops** | **13.8 s** | **65%** | 1.62 hops/turn × ~8.5 s/hop, from the hop histogram below |
| **debounce window** | **6.0 s** | **28%** | `MESSAGE_DEBOUNCE_SECONDS` default, unset in production, so exactly 6.0 s on every turn |
| Shopify live read | 0.44 s | 2% | `catalog.fetch_all()` timed three times from inside the Railway container: 455 / 417 / 438 ms, 211 variants, once per turn |
| Meta send + ingest | ~0.6 s | 3% | two Meta round trips per message (`mark_as_read` in the webhook, `send_text` after) |
| **this codebase** | **0.02 s** | **0.1%** | `bench_turn.py` with the model removed: 6–34 ms for a whole turn |
| **total** | **≈ 21.3 s** | | matches the reported "21 seconds to a full minute" |

### Where the model time actually is

Hops per turn, and what each count costs, from the same 213 turns:

| hops | turns | mean (s) | p50 | p90 | max |
|---|---|---|---|---|---|
| 1 | 100 | 10.18 | 7.27 | 22.29 | 67.57 |
| 2 | 97 | 17.02 | 15.56 | 34.35 | 53.36 |
| 3 | 14 | 31.52 | 27.43 | 57.79 | 64.92 |
| 4 | 2 | 42.36 | — | — | 46.58 |

Mean hops per turn: **1.62**. So one model round trip costs roughly
**7 s at the median and 8.5 s at the mean**, and the marginal cost of the
second hop is about the same as the first. `TOOL_LOOP_CAP` is 8 and nothing
has ever come close to it — the cap is not what is slow, the *per hop* cost
is, and after that the *number* of hops.

Tools called across those turns, most used first: `get_variants` (65),
`get_products` (47), `add_to_cart` (24), `get_categories` (19),
`get_shipping_fee` (17), `get_my_profile` (16), `confirm_order` (13),
`ask_governorate` (12), `get_size_chart` (4), then single figures.

### What is sent on every hop

| | chars | ≈ tokens |
|---|---|---|
| system prompt (whatsapp) | 15,695 | ~5,200 |
| system prompt (instagram_dm) | 16,170 | ~5,400 |
| tool declarations (19 tools) | 14,729 | ~3,700 |
| **fixed prefix, every hop** | | **~9,000** |

Plus the conversation itself: stored histories run to a mean of 45 messages
and a p95 of 267, of which `assistant/context.py` sends the last 24 verbatim
and up to 60 older ones compacted.

### The benchmark, with the model taken out

`python scripts/bench_turn.py --runs 1`, planned provider, in-memory Shopify
shelf, SQLite:

| scenario | turn (ms) |
|---|---|
| greeting | 11 |
| product_question | 12 |
| sizes | 15 |
| add_to_cart | 12 |
| confirm_order | 11 / 34 (the step that writes the order) |
| shipping_question | 6 |

Stage split across those nine turns: tools 36%, session save 9%, history load
7%, prompt build and context build both under 1%.

**This is the finding that decides everything after it.** The codebase's own
contribution to a 21-second reply is about 20 milliseconds — one tenth of one
percent. There is no slow loop to find, no N+1 query worth chasing, no
serialisation to unpick. Every second the customer waits is a network round
trip or a deliberate wait, and the only optimisations worth making are the
ones that remove a round trip, shorten one, or stop waiting.

### Production configuration at the time of the baseline

`LLM_PROVIDER=openrouter`, `LLM_MODEL=z-ai/glm-5.3-flash`,
`LLM_MEDIA_MODEL=google/gemini-3.1-flash-lite`,
`OPENROUTER_PROVIDERS=z-ai,deepinfra,novita`,
`OPENROUTER_QUANTIZATIONS=fp8,bf16,fp16`. `MESSAGE_DEBOUNCE_SECONDS`,
`TOOL_LOOP_CAP`, `MODEL_CONTEXT_*` and `MESSAGE_WORKERS` are all unset, so all
four run on their defaults (6.0 s, 8, 24/60, 8 threads). One replica, Railway
region **ams** (Amsterdam), trial plan, `uvicorn app:app`.

### Judgement calls made while measuring

* **The baseline comes from the transcript, not from the logs.** There was no
  timing instrumentation before this work, so the logs could not say where a
  turn's time went — but every message has carried `at` since receipts
  shipped, and that is a direct measurement of 213 real customer turns rather
  than a sample of whatever was still in the log buffer. The logs are what the
  *post-change* numbers come from, now that there is something in them to read.
* **A gap over ten minutes is not a slow turn.** It is a paused conversation, a
  crashed turn answered by the next message, or a customer who came back the
  next morning. `MAX_PLAUSIBLE_SECONDS` in `scripts/prod_turn_latency.py`
  drops them; nothing the model has ever done comes close to the cut.
* **`by="system"` messages are not answers.** A status push or a cart nudge is
  written whenever the shop decided it, and counting one as a reply to the
  customer's last message produces gaps measured in hours.
* **The benchmark never touches the real Shopify store.** The order scenario
  runs against the suite's in-memory shelf (`tests/fake_shopify.py`), because
  a benchmark that places real cash-on-delivery orders and decrements real
  stock on every run is not a benchmark. `--live-shopify` exists for read
  timing and drops the order scenario when it is used.
* **No optimisation was allowed to change what a reply means.**
  `scripts/quality_gate.py` is what enforces that, and a change that fails it
  is reverted however much time it saved.

---

## Iterations

Each entry: what changed, the numbers before and after, and whether it was
kept. Reverts stay in the git history rather than being squashed away.

### Iteration 1 — the debounce window waits only as long as it has to

**Ranked first because:** 6.0 s on every single reply, 28% of the total, and
the only large term nothing outside this process had a say in.

**The measurement that decided it.** `mids` on a stored user message is every
platform id the debounced batch collected, so its length *is* how many
fragments that turn was assembled from. Across 254 production turns:

| fragments in the batch | turns |
|---|---|
| 1 | 249 (98.0%) |
| 2 | 3 |
| 3 | 1 |
| 6 | 1 |

Ninety-eight percent of replies were paying the full six seconds to catch the
other two percent.

**The change.** `MessageDispatcher._wait_for`: a batch waits
`MESSAGE_DEBOUNCE_FIRST_SECONDS` (2 s) until a *second* fragment arrives, and
from then on `MESSAGE_DEBOUNCE_SECONDS` (6 s) measured from the newest
fragment — which is byte-for-byte the old behaviour. A new
`MESSAGE_DEBOUNCE_MAX_SECONDS` (15 s) caps a batch's total age, which a fixed
window never needed and an extending one does. The short wait is clamped so it
can never exceed the full one.

**Before / after**, measured on the dispatcher with the production settings
(median of three runs, submit → handler):

| batch | before | after | change |
|---|---|---|---|
| 1 message | 6.02 s | **2.01 s** | **−4.01 s** |
| 2 messages, 1 s apart | 7.00 s | 7.02 s | unchanged |
| 3 messages, 1 s apart | 8.01 s | 8.01 s | unchanged |

Weighted by the distribution above, the mean debounce goes from 6.00 s to
**2.09 s**: **−3.9 s off a 21.3 s reply, −18.4%**.

**The cost, stated plainly.** A customer whose second fragment arrives between
2 and 6 seconds after the first now gets two turns instead of one — an extra
model call, and a reply that reads as two messages rather than one. Not a
wrong answer, and the conversation lock still serialises them. Three of 254
measured turns are in that shape at most.

**Quality gate:** passed. **Suite:** green. **Flag:** `ADAPTIVE_DEBOUNCE`,
default **on** — the gate was clean and the fragment behaviour above 2 s is
unchanged. `ADAPTIVE_DEBOUNCE=0` restores the old fixed window with no deploy.

**Kept.**

### Iteration 2 — stop paying for reasoning the shop does not need

**Ranked first because:** after iteration 1 the model is 79% of what is left,
and a single round trip costs ~7 s at the median. Nothing else is within an
order of magnitude.

**The measurement that decided it.** Instrumenting the OpenRouter usage payload
(iteration 0) showed where the generated tokens go. On an ordinary shop turn:

| | mean | p50 | p90 |
|---|---|---|---|
| prompt tokens | 10,849 | 10,623 | 12,272 |
| **of which cached** | **10,101 (93%)** | 10,304 | 10,688 |
| completion tokens | 454 | 255 | 1,638 |
| **of which reasoning** | **416 (92%)** | 239 | 1,555 |

Two findings in one table. The first killed a hypothesis: **prompt caching is
already happening**, automatically, on 93% of the prefix — there is nothing to
win there (see "Hypotheses that measurement killed"). The second is this
iteration: **92% of everything this model generates is thinking, not answer.**
The reply itself is 30–45 tokens.

Then, against the shop's own prompt and tool declarations, three samples each:

| request | median | reasoning tokens |
|---|---|---|
| baseline (no `reasoning` field) | 5,698 ms | 49–120 |
| `reasoning {"effort": "low"}` | **4,647 ms** | **0** |
| `reasoning {"effort": "minimal"}` | 5,389 ms | 0 |
| `reasoning {"max_tokens": 128}` | 4,778 ms | 0 |
| `reasoning {"enabled": false}` | HTTP 400 | — |

The endpoint refuses to be told *not* to think and accepts being told how
hard. The existing comment in `openrouter.py` had tested only the first of
those, which is how a fifth of every round trip stayed invisible.

**The change.** `OpenRouterProvider._reasoning()` sends
`reasoning: {"effort": OPENROUTER_REASONING_EFFORT}`, default `low`. A blank
value sends no `reasoning` field at all — byte-for-byte the request this made
before — and is the way back with no deploy. `temperature`, the routing filter
and `max_tokens` are untouched; a test pins that.

**Before / after**, the six scenarios × 3 runs = 27 real turns, run from
inside the Railway container against the real model and the real network:

| | before | after | change |
|---|---|---|---|
| turn mean | 20,931 ms | **8,536 ms** | **−59.2%** |
| turn p50 | 19,281 ms | **7,477 ms** | −61.2% |
| turn p90 | 38,239 ms | 14,198 ms | −62.9% |
| turn max | 58,826 ms | 19,264 ms | −67.2% |
| llm share of the turn | 99.8% | 99.7% | unchanged |

Per scenario, after: greeting 4.3 s, shipping question 3.2 s, product question
7.5 s, sizes 11.4 s, add to cart 11.5 s, order 9.1 s.

**Quality gate:** **passed** against a golden set recorded on the *previous*
settings — 18 replies, 9 steps, no errors, no fallbacks, every price and size
still stated, still Egyptian Arabic, nothing truncated. The replies were also
read by hand, because this is the one change that trades judgement for speed:
the model still asks which colour when none was named, still catches a
governorate that disagrees with the address, still reads the order back before
placing it, and still quotes 590 / 60 / 650 correctly. Route variation between
runs (`get_products` alone vs `get_products` + `get_variants` for a sizing
question) is reported as a note, not a failure — see the gate's own docstring.

**Suite:** green. **Flag:** `OPENROUTER_REASONING_EFFORT`, default **`low`** —
the gate was clean on two runs and the hand read confirmed it.
`OPENROUTER_REASONING_EFFORT=` (blank) restores the old request with no deploy.

**Kept.**

### Iteration 3 — a single-match search carries its variants — **REVERTED**

**Ranked first because:** with reasoning dealt with, a round trip is what is
left. A probe isolating the remaining per-hop cost found a warm hop is ~1.9 s
and that **prompt size is not what drives it**: removing all 19 tool
declarations (10,326 → 6,772 prompt tokens, a third of the request) moved the
median from 1,902 ms to 1,732 ms. 170 ms for 3,554 tokens. So the lever is the
*number* of hops, not their size.

**The change.** A `get_products` search that matched exactly **one** product
handed that product's variants back with it — the same variant_ids, prices and
availability `get_variants` returns — so "what sizes does it come in" could be
answered in one round trip instead of two. Only on a single match: two or more
results is browsing. 15 of the catalogue's 18 product names search to exactly
one product, so this was the common case, not an edge.

**Before / after**, 27 real turns:

| | before | after | change |
|---|---|---|---|
| turn mean | 8,536 ms | 7,024 ms | **−17.7%** |
| turn p50 | 7,477 ms | 6,585 ms | −11.9% |
| tool calls p90 | 3 | 2 | |
| hops p90 | 4 | 3 | |

The mechanism worked exactly as designed.

**Quality gate: FAILED.** And it failed on the thing the change was always
going to risk: `get_variants` is the only tool that attaches a photograph, so
a model that no longer needs to call it no longer sends one.

```
- sizes[0]:         the golden run sent 2 photo(s) and this one sent none
- add_to_cart[0]:   the golden run sent 2 photo(s) and this one sent none
- confirm_order[0]: the golden run sent 2 photo(s) and this one sent none
```

A customer asking about a t-shirt in a clothes shop and getting a correct,
faster, picture-less answer is a worse reply. That is the rule this loop runs
under: when fast and correct disagree, correct wins.

**Reverted.** The commit stays in the history with its measurements, because
the idea is repairable and the numbers are worth having.

**What was kept from it:** the gate itself. `scripts/quality_gate.py` now fails
any change where the golden run sent photographs and the new one sends none,
and `bench_turn.py` records the count. That check did not exist before this
iteration, which is exactly why an iteration was needed to find it.

**The repair, not attempted here:** have a single-match `get_products` attach
the photo and the size chart the way `get_variants` does. That keeps the saved
round trip *and* the picture. It is left as an option rather than done, for two
reasons: it makes `get_products` a photo-sending tool, which is a change to the
shop's image policy rather than to its latency (a customer asking "do you have
this?" would start receiving a picture unasked), and the brief for this work
rules out refactors unrelated to latency. It is in the pull request as a
decision for the shop to take.

### Iteration 4 — the window remembers who actually writes in fragments

**Ranked first because:** after iteration 2 the split of an ~11.6 s reply is
model 8.5 s (73%), debounce 2.09 s (18%), Meta round trips 0.6 s (5%), Shopify
0.23 s (2%). Everything below the debounce is under the 3% bar (see
"Hypotheses that measurement killed"), and the model floor is not ours to move.
The debounce is.

**The problem with iteration 1's answer.** Two seconds was chosen as the
shortest wait that still gave a fragmenting customer a fair chance, and it had
to be, because the short window was the only thing standing between them and a
split reply. But the 2% who fragment are not a random 2% — writing in pieces
is a habit of a person.

**The change.** `MessageDispatcher` remembers a conversation that has written
in fragments and gives *that* conversation the full six seconds from its first
message, every time after. Two things count as evidence, and the second is the
one that matters:

* a batch that merged more than one message, and
* a message arriving within `MESSAGE_FRAGMENT_MEMORY_SECONDS` (20 s) of that
  conversation's previous batch being answered — which is exactly what a
  thought split by a window closing too early looks like from here. The
  customer it happened to is the customer it must not happen to twice.

Held for a day (`MESSAGE_FRAGMENT_MEMORY_TTL_SECONDS`), capped at 5,000
conversations and pruned by age, because a dict keyed on customer that only
grows is the same invisible leak the conversation locks were fixed for.

That makes the default safe to halve: `MESSAGE_DEBOUNCE_FIRST_SECONDS` 2 s → **1 s**.

**Before / after**, measured on the dispatcher at production settings (median
of three, submit → handler):

| batch | original fixed window | after iteration 1 | after iteration 4 |
|---|---|---|---|
| 1 message, customer never seen fragmenting | 6.00 s | 2.01 s | **1.00 s** |
| 1 message, **known fragmenter** | 6.00 s | 2.01 s | **6.00 s** |
| 2 messages, 1 s apart | 7.00 s | 7.02 s | 7.02 s |
| 3 messages, 1 s apart | 8.01 s | 8.01 s | 8.02 s |

**−1.0 s on 98% of replies, −8.6% of an 11.6 s total** — and the rare case is
served *better* than it was before any of this work, not worse: a known
fragmenter now gets the full six seconds from their first message, which even
the original fixed window only gave them from their second.

**Quality gate:** passed. **Suite:** green (four new dispatcher tests, including
one that pins the memory cannot grow without bound). **Flag:**
`ADAPTIVE_DEBOUNCE=0` still restores the original fixed window;
`MESSAGE_DEBOUNCE_FIRST_SECONDS=2` restores iteration 1's.

**Kept.**

### Iteration 5 — nothing the answer does not depend on runs before the answer

**Ranked first because:** what is left after iteration 4 is model 8.5 s (82%),
debounce 1.09 s (10%), Meta round trips 0.6 s (6%), Shopify 0.21 s (2%). The
model floor is not ours; the debounce has had two passes. The network calls
are one idea, not two: **work the reply does not wait for was sitting in front
of the reply.**

**What was measured first.** Graph and Shopify round trips, timed from inside
the Railway container:

| call | median |
|---|---|
| WhatsApp `GET /{phone_number_id}` | 196 ms |
| Instagram `GET /{account_id}` | 135 ms |
| Shopify `fetch_all` (211 variants) | 419 ms |

And where each one sat:

* `mark_as_read` ran **in the webhook, before `dispatcher.submit`** — so the
  debounce window did not even start until Meta had answered. 196 ms on every
  WhatsApp reply.
* Instagram did three of them there: `mark_seen`, `typing_on`, and the
  once-per-customer handle lookup. ~270–400 ms before the process started
  thinking.
* The Shopify read happened wherever the first catalog tool asked for it —
  which is *after* the model has come back saying which tool to call. Squarely
  on the critical path.

**The change.** `common/offthread.py`: a small bounded pool for work the
customer would not notice never happened. The read receipt, the typing
indicator and the handle lookup go there — after the transcript write, never
before it. And `shopify_catalog.prefetch()`, called when the turn opens, starts
the live read so it overlaps the first model hop (~2.4 s) instead of following
it.

The prefetch is **not** a cache and deliberately not one: the snapshot is still
read once per message and thrown away with the turn. `catalog.live_stock` reads
through it and `add_to_cart` decides whether a sale may happen on what it says
— a 30-second cache would have been cheaper and would have let a sold-out size
be sold. What it does cost is a Shopify call on turns that would never have
made one (a greeting); `SHOPIFY_PREFETCH=0` is the way out.

**Before / after.** The prefetch, measured with real Shopify reads in the loop
(`--live-shopify`, which skips the order scenario so no real order is placed):

| | before | after |
|---|---|---|
| `shopify` stage | 419 ms mean on 9 of 18 turns | — |
| `shopify_wait` (what the turn actually stood still for) | — | **0 ms** |

The read is fully hidden. The turn totals moved 7,841 → 6,319 ms mean in the
same pair of runs, but most of that is model variance between runs and is not
claimed here: **the attributable saving is 419 ms on the turns that read the
shelf, 210 ms per average turn.**

The Meta calls do not appear in the benchmark at all — it does not go through
a webhook — so they are stated from the direct measurement above: 196 ms per
WhatsApp turn, ~270 ms per Instagram turn, weighted by the production split
(129 WhatsApp / 84 Instagram) = **225 ms per average turn.**

Together **435 ms off a ~10.2 s reply, −4.3%.**

`shopify_wait` is new instrumentation added in the same commit, and it is there
because a prefetched read leaves *no* `shopify` stage on the turn's line — which
reads as "the shelf was free" when what happened is "the shelf was paid for
somewhere else". The honest number is how long the turn stood still for it.

**Quality gate:** passed — after fixing the gate, which is worth writing down.
It first failed on `confirm_order[1]`: the golden run called `add_to_cart`
there and this one called nothing. Reading the transcript, this run had already
put the piece in the cart on the *previous* message — a better reply, not a
worse one — and the change under test does not touch the prompt, the tools or
the model, so it cannot have caused a different route. The rule was wrong, not
the change: "answered without looking anything up" is a property of a
**conversation**, not of a message. It now judges per scenario, and a check
that the rewritten rule still catches a reply that states a price having called
nothing at all is part of this commit.

**Suite:** green. **Flags:** `SHOPIFY_PREFETCH` (default on). The off-thread
courtesy calls have no flag: there is no configuration in which a customer
benefits from their reply waiting on their own read receipt, and
`common/offthread.py` falls back to running inline if the pool is shutting
down.

**Kept.**

<!-- ITERATIONS -->
