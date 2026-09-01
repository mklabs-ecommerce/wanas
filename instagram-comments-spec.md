# Instagram comment handling — implementation spec

Work in the Wanas Gallery repo. Read `CLAUDE.md`, `AGENTS.md`, and
`docs/OPERATIONS.md` first. Everything below extends the existing comment
path in `assistant/channels/instagram.py::_accept_comment` — it is a change
to a shipped design, not a new subsystem.

Follow the repo's existing conventions exactly: comments that say *why* a
rule exists, `settings`-driven config with safe defaults, one test file per
subject, no vendor SDKs above `assistant/providers/`.

## Scope guard — do NOT do these

Two ideas live in `instagram-engagement-plan.md` and are deliberately OUT of
scope. Do not implement them, do not refactor toward them:

- A global outbound throttle (~200/hour across all commenters). Only the
  per-commenter limits below exist.
- Resolving a post's image to a product (`identify_product_from_image`,
  a `post_product_links` cache table). The shipped behaviour — reading the
  post *caption* live via `InstagramClient.get_media`, never cached — stays
  exactly as it is.

Also out of scope: any owner-notification channel (email/WhatsApp). Every
alert below goes to the existing dashboard queue via `queues.enqueue`,
same as `comment_flood` and `negative_comment` do today.

---

## 1. A deterministic FAQ matcher, above the model

Some comments ask a real question whose answer is **fixed and identical for
every customer** ("التوصيل بياخد قد إيه؟", "الشحن بكام؟", "بتاخدوا كاش؟").
Today these classify as `important` and cost a DM each, when one public
sentence answers them completely and better.

Create a new module for this — suggested `assistant/comment_faq.py` (put it
wherever it fits the layering; it must not import `dashboard/`).

**It is a lookup, not a classifier. No model call, ever.** The public surface
must never display a sentence a model chose. This is the same principle the
existing fixed `PUBLIC_ACKS` follows, and the same one
`domain/services/search_terms.py` states in its docstring: the rule lives
below the model, where it is a rule.

**Matching:** normalise the comment text with the existing Arabic/franco
normaliser in `domain/services/search_terms.py` (`normalize` — it folds
diacritics, أ/إ/آ→ا, ى→ي, ة→ه, and franco spellings), then test it against
per-key pattern lists. Cover both Arabic and franco spellings for each key.
Write the patterns so a comment naming a *product* alongside the question
("الهودي الأسود ده بكام؟") does **not** match `shipping_cost` — that is a
product question and must reach the model as `important`.

**The three keys, with their exact public replies:**

| key | matches | public reply (verbatim) |
|---|---|---|
| `delivery_time` | how long delivery takes | `التوصيل بياخد لغاية 4 أيام لكل محافظات مصر 🖤` |
| `shipping_cost` | how much shipping costs (no product named) | `الشحن 110 جنيه لكل محافظات مصر 🖤` |
| `payment` | payment methods, cash on delivery | `بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع 🖤` |

The 110 EGP figure is a hardcoded string on purpose: shipping is a flat rate
to every governorate, confirmed across ~100 completed orders. Note for context
that the live fee the bot quotes in DM comes from the `ShippingRate` table in
Postgres (`domain/services/shipping.py::get_fee`), **not** from Shopify — if
that flat rate ever changes, this string changes with it.

Do not put a URL in the `payment` reply — Instagram suppresses reach on
comments carrying links.

**Behaviour on a match:**

1. Reply publicly with that exact string. Nothing else — **no DM, no private
   reply, no session seeded.** The question is answered; opening a DM for it
   is the noise this whole change removes.
2. Write an `InstagramCommentReply` row **before sending**, exactly as the
   `important` path does today, with `public_replied` set on success. This is
   the one-reply-ever guarantee: a crash between write and send, or a
   duplicate webhook delivery, must never put the same reply under a
   customer's comment twice.
3. Return. The classifier is never called for this comment — that is the
   saving.

**Its own rate limit, separate from the DM one.** Add
`INSTAGRAM_FAQ_RATE_LIMIT`, default **5** per commenter per rolling hour,
counted independently of `INSTAGRAM_COMMENT_RATE_LIMIT` (3). Rationale: the
3/hour cap exists to stop a flood of *DMs*; an FAQ reply sends no DM and
costs no model call, so it should not consume that budget — but it is still
visible under a post, so it is not unlimited either. Over the limit: drop
silently with an INFO log, no alert (this is not a flood, just a chatty
commenter).

**When `INSTAGRAM_PUBLIC_REPLY_ENABLED=0`:** an FAQ match must **fall through
to the `important` path** (public ack skipped as usual, private reply + DM
seed as usual). That flag means "do not speak in public", not "ignore the
customer" — the question still deserves an answer.

**Ordering:** the FAQ matcher runs after the whole existing filter chain
(disabled / own account / parent_id / duplicate claim / too old / rate limit
/ says-nothing) and **before** `_classify`.

## 2. Two new classifier categories

`COMMENT_CATEGORIES` becomes six: `important`, `positive`, `negative`,
`complaint`, `spam`, `neither`. Update `_COMMENT_INSTRUCTION` in
`assistant/providers/openrouter.py` (and the Gemini provider if it declares
the same method) to describe all six in Egyptian Arabic, matching the
existing prompt's style. Keep the JSON-only, no-tools, `max_tokens: 64`,
`temperature: 0.0` shape — do not turn this into an agent call.

**`complaint` — a real customer with a real problem.** ("بقالي أسبوع
مستلمتش", "وصلني مقاس غلط"). This is split out of `negative` because the two
deserve opposite treatment: a hater ignored is fine, a paying customer
ignored in public is the worst outcome on this surface. Actions:

1. A short fixed public reply — **not model-written, and it must not admit
   fault**, because nobody has checked the order yet. Use:
   `بعتنالك في الدايركت عشان نظبطها فوراً 🖤`
   (respects `INSTAGRAM_PUBLIC_REPLY_ENABLED` like every other public reply)
2. The private reply + DM handoff, exactly as `important` does — same opener
   mechanics, same session seeding, same post-caption context.
3. `queues.enqueue` with reason `customer_complaint`, the comment text in the
   payload, flagged so staff see it as higher priority than
   `negative_comment`.

**`spam` — follower bots, scam links, crypto.** Action: **`queues.enqueue`
with reason `spam_comment` and nothing else.** No public reply, no DM, and
**do not hide the comment.** `InstagramClient.hide_comment` exists and stays
uncalled deliberately: hiding is invisible to the shop, so a misclassified
real customer would vanish with no trace. The owner hides by hand from the
alert.

`negative` keeps its current behaviour unchanged (silent alert, no public
action, no DM). `positive` (like only) and `neither` (nothing) unchanged.

## 3. Classifier failure now means silence, not a DM

`_classify` currently falls back to `"important"` when the provider is
unavailable. Reverse it: **fall back to `"neither"`** — no public reply, no
DM — **and `queues.enqueue` an alert with reason `classifier_unavailable`,
carrying the comment text, comment id, media id and commenter id in the
payload.**

Rationale: the old fallback made a provider outage produce a burst of public
replies and DMs on a live post, with no model deciding any of them. Silence
is the safe failure on a public surface. The alert is what keeps silence from
meaning loss — the owner can work through the queue and answer by hand.

This applies to both the `ProviderError` branch and the bare `except` branch.
Update the docstring, which currently argues for the opposite.

## 4. A separate model for classification

Add `COMMENT_CLASSIFIER_MODEL` to `config/settings.py` and `.env.example`,
**empty by default**. In `classify_comment`, send
`settings.comment_classifier_model or self.model`.

Empty default means today's behaviour is bit-for-bit unchanged. The point is
not cost — the call is ~250 input / 20 output tokens and rounds to nothing.
The point is decoupling: without it, upgrading the chat model silently
changes classification on a live public surface, and a model being pulled or
rate-limited takes down chat and comments together. Document that reasoning
in the `.env.example` comment, in the style of the surrounding entries.

## 5. Replies under our own reply

`_accept_comment` currently drops every comment carrying a `parent_id`.
That was safe when the bot's only public output was "شوف الدايركت", which
nobody answers. With FAQ answers going out publicly, a customer following up
under our reply ("طب والشحن بكام؟") is now both likely and reasonable.

Change the rule to: if `parent_id` matches a row in `InstagramCommentReply`
— i.e. that parent is a comment **we** replied to — process the comment
normally through the full chain. Any other `parent_id` (a reply between two
other people, in a thread that is none of the shop's business) keeps being
dropped, with the existing log line.

Do not touch the own-account check that runs before this — it stays first, so
this change cannot open a self-reply loop.

## 6. Fix: emoji-only comments consume the rate-limit budget

In `_accept_comment` today, the `InstagramCommentReply` row is written inside
the rate-limit block (step 6) and the `_comment_says_nothing` check runs
after it (step 7). So a "🔥" comment burns one of the commenter's three
hourly slots while receiving nothing.

Move the `_comment_says_nothing` check **above** the `session_scope` block
that does the rate-limit count and the row insert. The duplicate claim
(`claim_message("igc:...")`) stays where it is, before everything — an
emoji comment should still be claimed so a redelivery is not re-examined.

## Tests

Extend `tests/test_instagram_comments.py` (and add to
`tests/test_openrouter_provider.py` for the classifier changes). Cover:

- Each FAQ key matches its Arabic **and** franco spellings and produces the
  exact public string, with no DM sent and no session created.
- "الهودي ده بكام؟" (product named) does **not** match `shipping_cost` and
  reaches the classifier as a product question.
- An FAQ reply writes its `InstagramCommentReply` row; a duplicate webhook
  delivery of the same comment sends nothing a second time.
- FAQ replies do not decrement the DM rate-limit budget: 5 FAQ comments then
  a product question from the same commenter still gets its DM.
- The 6th FAQ comment in an hour is dropped.
- `INSTAGRAM_PUBLIC_REPLY_ENABLED=0` turns an FAQ match into a DM handoff.
- `complaint` produces public reply + DM + a `customer_complaint` alert.
- `spam` produces an alert and **no** call to `hide_comment`, no public
  reply, no DM.
- A provider raising `ProviderError` produces no public reply, no DM, and a
  `classifier_unavailable` alert.
- `COMMENT_CLASSIFIER_MODEL` set changes the `model` field in the request
  body; unset falls back to `LLM_MODEL`.
- A reply whose `parent_id` is a comment we replied to is processed; a reply
  with an unknown `parent_id` is dropped.
- An emoji-only comment does not create an `InstagramCommentReply` row and
  does not consume rate-limit budget.

Run `make check` (ruff + full suite) and make it green. If any new alert
reason surfaces in the dashboard UI, add its translations —
`tests/test_dashboard_i18n.py` fails otherwise.

## Docs

Update in the same change:

- `.env.example` — `INSTAGRAM_FAQ_RATE_LIMIT`, `COMMENT_CLASSIFIER_MODEL`,
  with the reasoning comments the file's style calls for.
- `docs/OPERATIONS.md` — the "Launching the Instagram channel" section: the
  new categories, that the FAQ answers publicly without a DM, and that a
  classifier outage now shows up as `classifier_unavailable` alerts in the
  queue rather than as a burst of public replies.
- `CHANGELOG.md` — following the existing entry style.

## Unrelated, do not fix here — report only

While working you may notice these. Do not change them; mention them at the
end so the owner can decide separately:

1. `CLAUDE.md` states the shop is cash-on-delivery "with nothing to refund
   against", and there is no refund path anywhere. The owner says online
   payment through the website is also available, which contradicts that
   assumption.
