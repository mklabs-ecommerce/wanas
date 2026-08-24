# Wanas Gallery Chatbot — Instagram Comment Engagement (Addendum)

Extends the existing Modular Monolith chatbot with automatic handling of Instagram post/reel comments: important questions get a short public reply + an automatic DM handoff into the existing bot conversation; positive comments get a like reaction; negative comments are left alone publicly (optionally logged silently for the owner).

This is officially supported by Meta as **"Private Replies to Comments"** — not a workaround. Since Meta Business verification is already done, this is buildable now.

---

## 1. What "model" this actually needs — two different jobs, not one

Don't route comments through the full order-taking conversation agent. This is a **classification** problem first, and only sometimes becomes a **conversation** after that:

1. **Classify the comment** (cheap, fast, single-purpose call): is this an important question, a positive reaction, a negative comment, or neither? This should be a small, separate, JSON-only call — the same pattern already used for `identify_product_from_image` (no tools declared, strict JSON out). Reuse the existing Gemini integration client for this; it does not need the multi-round tool-calling agent.
2. **Only for "important question" comments**, hand off into the **existing chat agent** (same one used for regular DMs/web chat) to actually have the conversation — reusing `modules/chat` entirely, not rebuilding it. The Instagram commenter's DM thread just becomes a new `session_id`, exactly like a WhatsApp or web session would.

This keeps the expensive, careful, tool-using agent reserved for actual conversations, and keeps comment-triage cheap and fast — important given comment volume can be much higher than DM volume.

---

## 2. Routing logic

| Comment type | Public action | Private action |
|---|---|---|
| **Important** (asks about product, price, availability, sizing, order status, etc.) | Short, fixed template reply — e.g. "Thanks for reaching out! We've sent you a DM 💬" — **not** model-generated, for reliability and consistency in a public, irreversible space | Send a Private Reply (see Section 4) that opens a DM, then hand the conversation off to the existing chat agent, with product context resolved per Section 3 |
| **Positive** (hearts, emojis, "love this", general positive sentiment) | **Like the comment** (Meta's official like/unlike-comment endpoint) | None |
| **Negative** (complaints, negative sentiment, criticism) | **No public action** — do not reply, do not like | Silently logged via the existing `support` module as a low-priority ticket (category "Public comment — negative") so the owner has visibility without engaging publicly |
| **Neither** (spam, unrelated, unclear) | No action | No action |

**Why fixed templates for the public reply instead of letting the model write it:** a public comment reply is visible to everyone, permanent, and reflects on the brand — the failure mode of a model going slightly off-script here is much more visible and harder to walk back than in a private DM. Keep this deterministic.

---

## 3. Product Context Resolution (knowing which product a post/reel is about)

**The gap:** Instagram tells you who commented and on which post (`media_id`), but nothing tells you which Shopify product that post is showcasing. Without resolving this, "what's the price?" in a DM has no anchor — the bot would have to ask every time, even though the answer is visually obvious from the post itself.

**The fix: reuse the image-matching capability already being built** (Section 1's sibling feature, `identify_product_from_image`), applied to the post's own photo instead of a customer-submitted one.

### How it works

1. When a comment webhook arrives, check if this `media_id` has already been resolved to a product (cached lookup).
2. **If not cached:** fetch the post's own image via Instagram's Graph API (`GET /{media_id}` with the `media_url` field), then run it through the **same** `identify_product_from_image` matching used for customer photos — just pointed at the post's image instead. Cache the result against the `media_id` so this only runs once per post, not once per comment.
3. **If a confident match is found:** store it and use it as context (see below).
4. **If no confident match:** store that too (as "unresolved"), so the bot doesn't guess — it should ask the customer to clarify in the DM rather than assume, same honesty principle as the customer-photo matching flow.

### How the resolved product gets used

When an "important" comment triggers a DM handoff (Section 2), pass the resolved product as **context, not a canned answer** — the chat agent should still call `search_products` (or the relevant order/stock tool) to confirm live price/stock, never state a number from the cached match itself. The cached match only answers "which product," never "what does it cost right now" — that's still always a live lookup, so price changes or stock updates are never stale.

Suggested context framing for the chat agent's first message in the session:
> "This conversation started from a comment on a post featuring **[Product Title]**. If the customer asks about price, size, or stock without specifying a product, assume they mean this one — but confirm using your tools, and ask for clarification if they seem to mean something else."

If the post's product couldn't be confidently resolved (step 4), skip this context entirely — the bot should ask naturally, e.g. "Which piece did you have in mind?"

### Architecture addition

```
modules/
└── engagement/
    ├── repository.py        # ADD: post_product_links table
    │                         #   (media_id, product_handle, confidence, resolved_at)
    └── service.py            # ADD: resolve_post_product(media_id) —
                               #   checks cache, else fetches media image via
                               #   integrations/instagram/client.get_media(),
                               #   calls catalog.service.identify_product_from_image()
                               #   (SAME function customer photos use), caches result
```

**Boundary note:** `engagement` doesn't duplicate the image-matching logic — it calls `catalog.service.identify_product_from_image()`, the same function built for customer photos. One matching implementation, two callers (customer-submitted photos, and now post images too).

---

## 4. Instagram/Meta API facts that shape the build (verified against current Meta documentation and trackers, 2026)

- **Private Replies to Comments** is the specific, Meta-sanctioned API for "comment triggers a DM" — you must send it within **7 days** of the comment being posted, or the ability to open a DM from that comment is gone entirely.
- Once that first DM is sent, standard Instagram messaging rules apply to everything after: the conversation stays open indefinitely as long as the customer keeps replying (each reply resets a 24-hour window), same as your existing chat sessions already assume.
- **Liking a comment** is a separate, officially supported endpoint (added by Meta in April 2026) — straightforward, no messaging-window implications, safe to call anytime.
- **Rate limits:** Meta publishes 750 private replies/hour to comments, and 100 calls/second generally for the Instagram Login messaging path. Common practice is self-throttling well under that (~200/hour) — build in a simple rate limiter/queue rather than firing immediately on every webhook event, both for safety margin and to avoid bursty behavior looking automated/spammy.
- **Public replies should stay short** (1-2 sentences) — acknowledge, don't explain. This matches your own instruction and is also Meta ecosystem best practice.
- Only Business/Creator Instagram accounts connected to a Facebook Page get Graph API access for this — already satisfied since your verification is done.

---

## 5. Architecture (fits the existing Modular Monolith)

```
integrations/
└── instagram/
    └── client.py          # raw Graph API calls: reply_to_comment (public),
                            #   like_comment, send_private_reply (opens DM),
                            #   get_media (fetches a post's own image for
                            #   product-context resolution, Section 3)

modules/
└── engagement/             # NEW — comment classification + routing
    ├── service.py           # classify_comment(), route_comment(),
    │                        #   resolve_post_product() — Sections 2 & 3
    ├── repository.py        # post_product_links table (Section 3)
    ├── webhook_router.py    # receives Instagram comment webhook events
    └── schemas.py           # CommentEvent, CommentClassification data shapes
```

### Module boundary rules (same discipline as the rest of the project)

- `modules/engagement` **does not** talk to Shopify or run conversation logic itself. For "important" comments, it calls into `modules/chat`'s existing service to start/continue a conversation, and `catalog.service.identify_product_from_image()` for product resolution — it doesn't duplicate either.
- For negative-comment logging, it calls `modules/support`'s existing `create_ticket()` — it doesn't write to the tickets table directly.
- `integrations/instagram/client.py` is a dumb wrapper, same as the Shopify and Gemini clients — no business rules about what counts as "important" live there.

---

## 6. Environment variables needed

```
INSTAGRAM_ACCESS_TOKEN=
INSTAGRAM_BUSINESS_ACCOUNT_ID=
INSTAGRAM_WEBHOOK_VERIFY_TOKEN=      # for Meta's webhook subscription handshake
```

---

## 7. Suggested Build Order

1. `integrations/instagram/client.py` — implement `reply_to_comment()`, `like_comment()`, `send_private_reply()`, `get_media()` against Meta's Graph API. Test each function standalone against a real test comment/post before wiring up automation.
2. Webhook receiver (`modules/engagement/webhook_router.py`) — confirm you can receive and correctly parse real comment events end-to-end (log them, take no action yet).
3. `classify_comment()` — the lightweight Gemini classification call. Test it against a range of real/sample comments (positive, negative, important questions, spam) and check the classifications actually match your judgment before wiring up any automated action.
4. Wire up the **positive** path first (like_comment) — lowest risk, no messaging-window complexity, good way to confirm the pipeline works end-to-end.
5. `resolve_post_product()` (Section 3) — test it against a handful of real posts and confirm the matches are actually correct before it feeds into any live conversation.
6. Wire up the **important** path — public template reply + Private Reply API call + handoff into `modules/chat`, including the resolved product context from step 5. Test with a real comment from a second account before going live broadly.
7. Wire up the **negative** path's silent ticket logging.
8. Add the rate limiter/queue from Section 4 before this runs against real, uncontrolled comment volume.

---

## 8. Decisions (confirmed)

- **Negative comments:** silently logged as a low-priority support ticket (via `modules/support`'s existing `create_ticket()`) for the owner to review — no public reply, no DM.
- **DM opener:** references the specific comment/post the customer engaged with (e.g. "Hi! Saw your comment on our new hoodie post — what can I help with?"), not a generic opener. This means the classification step must pass the comment text (and ideally which post/reel it was on) through to the chat agent as context for its first message — combined with the resolved product from Section 3, the opener can be specific about the actual item, not just the post.
- **Tagged comments** (e.g. "@friend look at this!") do **not** count as "important" — they're the tagger drawing someone else's attention to a post, not the tagger themselves asking a question. No DM handoff triggers on these. If the *tagged* person separately comments with their own question, that's evaluated normally on its own merits. The classifier should treat a comment that's essentially just an @mention (with no real question/request from the commenter) as "neither," not "important."

## 9. Nothing currently open

All prior open questions are resolved (Section 8).
