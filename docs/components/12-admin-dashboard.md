# 12 — Admin dashboard

## Purpose

The staff-facing side of everything — order management, inventory control, client oversight, discount creation, and the review queues for anything the automated flows don't handle on their own.

## Access

**Username and password, one role.** Every staff member who can log in can do everything: edit stock and prices, advance an order, block a client, resolve a swap. The team is small enough that a permission matrix would cost more to maintain than it protects, and a role split invented now would be guesswork about how they actually work.

What is *not* optional at one role:

- **Every action is attributed.** Each staff-initiated change records who did it and when — see the audit trail below. With shared permissions, attribution is the only thing that answers "who dropped that price to 5 EGP."
- **Passwords are hashed,** sessions expire, and there is no shared team login. A shared account destroys attribution, which is the one control this model relies on.
- **The dashboard is never reachable without logging in.** It can edit stock and orders; an accidentally-public deployment is a live business risk, not a test inconvenience.

Roles are a later change, and a cheap one if attribution is in place from the start — the audit log tells you which actions actually need restricting.

## Core sections

- **Orders** — list/detail view, manual status updates (e.g. Packed → Shipped), full modification history per order.
- **Products & inventory** — CRUD for products, price and threshold editing, and **per-variant stock adjustment** (outside the order-driven Inventory service path). The variant grid is the screen staff will live in: "we ran out of olive in M" happens far more often than a product selling out entirely — **14 of the 17 products with stock are only partly buyable** — and a variant at zero is what stops the bot offering it. Note the grid is now two-dimensional per product (colour × size, and colour × size × length for the Worker Jacket), which is what the colour merge traded for a shorter product list.
- **Clients** — list view, ability to block a client, view of their order/feedback history.
- **Size charts** — view which of the 12 charts each of the 18 products uses and reassign it. Every product is mapped today, so this is a low-traffic screen, but a new product lands unmapped and someone has to fix that without a deploy. Editing the measurement numbers themselves can stay a data-file change for now.
- **Shipping rates** — the `governorate` → fee table the bot reads when quoting a total. Small, but it's the only place a delivery price can be changed, and an order can't be placed for a governorate with no rate set.
- **Discounts** — create/edit codes, see live usage stats per code.
- **Analytics** — see `13-analytics.md`.

## Review queues

Three things land here for a human to act on, rather than resolving automatically:

1. **Item-swap requests** — a customer wants to swap for a different product entirely. Staff sees the requested swap, checks stock for the new item, and approves (which then runs the same "apply automatically" logic as a quantity change) or rejects (customer is notified and the original order stands).
2. **Unclear chatbot conversations** — anything the agent couldn't resolve, plus any conversation where the customer asked for a person, and (in Phase 1) any incoming photo. Full message history attached, so staff aren't picking up context-free.
   - **The conversation is paused while it sits here.** The bot stops replying entirely; incoming messages are stored and shown to staff. Staff need two controls: a way to **reply to the customer as the shop**, and a **resolve** action that un-pauses the conversation and hands it back to the bot. Nothing else clears the pause — not a timer, and not the bot deciding things look normal again. See `02-chatbot.md` and `16-supporting-tables.md`.
3. **Custom product requests** — a customer sent a photo that didn't confidently match anything in the catalog (see `14-image-recognition.md`). Staff sees the photo, the AI's description of what it detected, and the customer's contact thread, then decides whether it's producible and replies directly — this is a business/craft judgment the system deliberately doesn't make on its own.

## Alert inbox

**In Phase 1 this is the only staff alert channel** — there is no email provider (see `AGENTS.md`), so new order confirmed, low stock, automatic modification and item-swap-awaiting-review all land here. A persistent, filterable record beats messages disappearing into an inbox anyway. Custom product requests join the list when image recognition ships.

## Interactions with other components

- **Reads** from all five databases, plus the Image Recognition component's match results.
- **Writes** for every staff-initiated action: approving/rejecting a swap, editing a product or threshold, creating a discount code, blocking a client, manually advancing an order's status.

## Edge cases worth knowing about

- **A product has 16 variants and staff need to change one** — the variant grid has to make a single row editable without a form per variant, or staff will stop keeping it accurate, which is the only failure mode that matters here.
- **Two staff members act on the same order at once** (e.g. both trying to approve the same swap) — whichever action lands first wins; the second sees a "this was already resolved" message rather than double-applying the change.
- **Audit trail** — every staff action worth tracking (status changes, swap decisions, threshold edits) should be logged with who did it and when, the same way automatic changes are logged — useful both for accountability and for debugging a disputed order later.
