# 05 — Facebook & Instagram (DM + public comments)

## Purpose

Two distinct automations on the same platform family: DM ordering/support (shared logic with WhatsApp and TikTok DM) and public comment auto-reply (unique to Facebook and Instagram — not available on TikTok).

## DM ordering & support

- Uses the same Chatbot Orchestrator as every other DM channel (see `02-chatbot.md`): ordering, modification/cancellation, feedback, stock questions.
- Customer identification uses the page-scoped user ID (PSID) Meta provides, mapped to `client_id` the same way as any other channel.
- No status-tracking pushes here — that stays exclusive to WhatsApp.

## Public comment auto-reply

A genuinely separate piece of logic from DMs — comments don't carry conversation state the way a DM thread does; each comment is evaluated independently.

**How it works:**
1. A new comment on a Facebook Page post or Instagram post triggers a webhook.
2. The comment text is checked against common patterns: stock/pricing questions, FAQs.
3. **Confident match** → an automatic reply is posted on the comment.
4. **Looks like a complaint** → flagged for a human instead of auto-replied — the bot doesn't attempt to handle anything emotionally sensitive or ambiguous.
5. **No match at all** → left alone; not every comment needs a reply.

This requires its own webhook subscription per platform (Facebook Page comments and Instagram post comments are separate subscriptions, even though the underlying logic is shared) and separate approval through Meta's app review process.

## Interactions with other components

- **Chatbot Orchestrator** — for DM conversations.
- **Product DB** — read for stock questions, both in DMs and in comment auto-replies.
- **Admin dashboard** — complaints and unclear DMs land in the human-handoff queue here.

## Edge cases worth knowing about

- **Reply-to-a-reply comment threads** — nested comments are still evaluated independently; the bot doesn't need to track a whole thread's context, just the individual comment.
- **Meta's API rate limits** — matters more for comment auto-reply than DMs, since a viral post can generate a comment spike; replies queue rather than firing instantly under load.
- **Distinguishing FB from IG** — same underlying logic, but the webhook and reply-posting calls are platform-specific, so this is really two thin adapters sharing one classification engine.
