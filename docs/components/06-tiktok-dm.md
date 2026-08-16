# 06 — TikTok DM

## Purpose

Ordering and support via DM only — TikTok has no official API for comments, so that side is intentionally out of scope (handled manually if needed).

## Platform constraints (recap, with implementation detail)

| Constraint | Effect on design |
|---|---|
| Verified Business Account required | Operational prerequisite before this channel can launch |
| Bot can't message first | The conversation can only ever be customer-initiated — no proactive TikTok outreach, ever, not even a status update |
| Links aren't clickable in DMs | Payment can't use a tappable link — see order code flow below |
| Not available in EEA/Switzerland/UK | Not a current concern, but relevant if the customer base ever expands there |
| Access via approved partner | Not a direct build against TikTok's raw API — unlike WhatsApp, where Phase 1 goes direct to Meta's Cloud API |

## Order code flow

Since a payment link can't be sent usably in a TikTok DM, the Payment service branch for TikTok orders works differently:

1. Customer builds their order in the DM conversation, same as any other channel (via the Chatbot Orchestrator).
2. At the payment step, instead of a link, the bot sends a short order code.
3. Customer opens the website, enters the code, and it pulls up their pre-built order — ready to pay.
4. The order sits in `Pending payment` the same as any card/wallet order, with the same 30-minute expiry window. If the code isn't used in time, the order cancels and stock releases, exactly like the link-based flow.

## Interactions with other components

- **Chatbot Orchestrator** — identical conversational logic to WhatsApp/FB DM/IG DM, since the agent loop, the tools, and session handling are all shared.
- **Payment service** — the one place TikTok genuinely branches from the other channels (order code instead of a link).
- **Product DB** — read for stock questions and live availability during ordering.

## Edge cases worth knowing about

- **Order code expiry** — same 30-minute window as pending payment generally, kept consistent rather than introducing a separate timer to track.
- **Mistyped codes** — the website's code-entry field should validate format before attempting a lookup, so a typo gives an immediate "check the code" message rather than a confusing failure.
- **Code reuse after expiry** — an expired code should not resolve to anything (the underlying order is already cancelled), so the customer is told clearly to start over rather than being shown a stale order.
