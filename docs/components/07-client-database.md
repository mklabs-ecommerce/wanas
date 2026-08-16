# 07 — Client database

## Purpose

The single record of who's ordering — whether they have an account or not.

## Fields

| Field | Type | Notes |
|---|---|---|
| `client_id` | Primary key | |
| `full_name` | Text | |
| `phone` | Text | Always present — required at checkout on every channel |
| `email` | Text, nullable | **Null for every Phase 1 order** — the WhatsApp flow never asks for one. Required once the website ships |
| `address` | Text | |
| `has_account` | Boolean | Distinguishes registered vs guest |
| `status` | Enum | `active` / `blocked` — blocked reserved for abuse cases, not an approval gate |

## How it's written to

- **Order service**, as part of the atomic order-creation transaction (see `01-backend-platform.md`) — creates a new record for a first-time customer, or updates one for a returning customer.
- **Chatbot Orchestrator**, for the channel-identity mapping table that links a WhatsApp number/PSID/TikTok ID to a `client_id` (see `02-chatbot.md`).
- **Admin dashboard**, when staff manually block a client.

## Behavior for guests (decided)

Without an account, there's a real question: does every guest order create a brand-new `client_id`, fragmenting one person's order history across many disconnected records?

**Decision: match, then ask — never link silently.** When a guest checks out and their phone or email is an exact match for an existing client record, the customer is asked to confirm it's them ("Is this you? We'll remember your address") before the records are linked. Confirmed → reuse the existing `client_id` (still `has_account = false`), so order history and feedback stay attached to one person. Declined or ignored → a fresh record, and the two are left separate.

**Why not link automatically.** An automatic link is the same operation as showing one customer another customer's saved address and order history. Phone numbers get reused by carriers, shared within a household, and mistyped, so an exact match is not proof of identity — and the failure is silent and one-directional, since the person whose data leaked never finds out. Confirming costs one message and removes that whole class of failure.

This matches the cross-channel identity rule in `02-chatbot.md`, which reaches the same conclusion for the same reason: customer-confirmed links, not automatic guesses. Both paths use the same confirmation step, so there's one behavior to build and one to explain.

## Relationships

- **Order DB** — one client can have many orders (`client_id` foreign key).
- **Order Feedback** — one client can leave many feedback entries, one per order.
- **Chatbot mapping table** — one client can be linked to multiple channel identities (WhatsApp, Instagram, etc.), though not auto-merged (see `02-chatbot.md`).

## Edge cases worth knowing about

- **A blocked client tries to order** — the Order service checks `status` before allowing a new order to proceed, independent of which channel they use.
- **Same person, different phone number later** — won't automatically match; they'd end up as a second guest record unless they create an account, which is a reasonable trade-off for avoiding false matches.
