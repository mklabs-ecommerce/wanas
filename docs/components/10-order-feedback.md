# 10 — Order feedback

## Purpose

Feedback lives per order, not per client, since one client can order — and leave feedback — many times.

## Fields

| Field | Type | Notes |
|---|---|---|
| `feedback_id` | Primary key | |
| `order_id` | Foreign key | One row per order |
| `client_id` | Foreign key | |
| `rating` | Integer | Star rating |
| `text` | Text | Free text |
| `date` | Timestamp | |

## How it's triggered

The Notification service fires a feedback request the moment an order's status reaches `Delivered` (see `01-backend-platform.md`) — **always via WhatsApp**, to the phone number on the order, regardless of which channel placed it.

Not "whichever channel the customer used": TikTok doesn't permit business-initiated messages at all (see `06-tiktok-dm.md`), so a TikTok-placed order's feedback request would simply never arrive. WhatsApp is already the universal channel for proactive messages — order confirmations and status pushes both work this way (`04-whatsapp-channel.md`) — and the phone number is a required field on every channel, so it's always there to send to.

## How it's written to

The agent, via `submit_feedback(order_id, rating, text?)` — see `15-tool-contracts.md`. A rating alone is enough to save a record; free text is optional on top of it. Only accepted for a `Delivered` order, and only once per order.

## How it's read

- Customer's own order history (website), to show what they said about a past order
- Admin dashboard, for spot-checking recent feedback
- Analytics — average rating over time, rating distribution, sentiment trends from the free text (`13-analytics.md`)

## Edge cases worth knowing about

- **Customer never responds to the feedback request** — no record is created; there's no follow-up nag built in by default, to avoid feeling like spam (worth deciding later if a single reminder is wanted).
- **Customer replies to the feedback request well after it was sent** — still accepted and linked to the correct order, since the request itself carries the `order_id` in its conversation context.
