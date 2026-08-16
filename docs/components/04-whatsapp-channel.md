# 04 — WhatsApp channel

## Purpose

The most full-featured chat channel — ordering, live status tracking, modification/cancellation, feedback, and stock questions — and, with the new adjustment, the **universal order-confirmation channel** for every order regardless of which channel placed it.

## Platform integration

- **Direct against Meta's WhatsApp Cloud API.** This is the decision for Phase 1 and it is what the test plan depends on: Meta's free test number with verified test recipients lets the whole flow run end to end before business verification completes. A Business Solution Provider (Twilio, 360dialog, Gupshup) is less setup and handles compliance for you, but it can't be tested for free and it isn't needed at this volume. Revisit at launch scale — the swap touches the send call, the webhook signature check, and template submission, and nothing else, provided the channel adapter stays behind the same interface the other channels use.
- Business-initiated messages (anything WhatsApp sends first, outside of an active customer conversation) generally require **pre-approved message templates** under WhatsApp's policy — this applies to status updates, feedback requests, and order confirmations, since they're proactive rather than a reply. Templates need to be submitted and approved ahead of launch, not built and hoped for.

## Order confirmation message *(new)*

- **Trigger:** the `order_confirmed` event from the backend (see `01-backend-platform.md`).
- **Sent to:** the phone number collected at checkout — always present, since it's a required field on every channel, not just WhatsApp. This means a website or TikTok order still gets a WhatsApp confirmation.
- **Content:** order number, items, total, and expected next update — kept inside the approved template's variable slots.
- **Each item line must carry its variant** — size and colour always, plus length on the Worker Jacket (see `09-order-database.md`). "1× WANAS Hoodie" is not a confirmable order line; "1× WANAS Hoodie — Olive, M" is. This matters for the template design specifically, because WhatsApp templates have hard limits on variables and length (see below), so the item list has to be pre-rendered into a single variable rather than given one slot per field.
- **Sale prices:** most of the catalog is discounted, so the total alone hides what the customer thinks they saved. Show the amount saved as one line rather than a struck-through price per item, which templates render poorly.
- **Alongside:** a confirmation email goes out too, but only when an email was collected — which in Phase 1 is never, since the WhatsApp flow doesn't ask for one. WhatsApp is the confirmation.

## Sending images

The agent can return attachments with a reply (see `02-chatbot.md`) — in Phase 1 that means **size charts**, sent whenever a customer asks about sizing for a product that has one.

- Sent as a real WhatsApp **image message**, not a link. A link in a DM looks like spam and often isn't tapped.
- **This is a reply inside an open conversation, so no approved template is needed** — the template rules only apply to business-initiated messages. Sizing answers are always replies, which is why this works without waiting on Meta.
- Upload each chart to Meta once and cache the returned media ID rather than re-uploading per message. There are only 12, they change rarely, and re-uploading a several-hundred-KB PNG on every sizing question is a slow reply for no reason.
- **Text carries the answer, the image supports it.** Send the numbers for the size they asked about in the message body too — a customer on a slow connection, or one who never opens the image, still gets an answer.

## Order tracking pushes

- As status changes (Confirmed → Packed → Shipped → Delivered), a template message goes out automatically at each transition.
- This is WhatsApp's exclusive responsibility — no other channel pushes status updates proactively.

## Feedback request

- Fired once status reaches `Delivered`. Asks for a star rating and free text, written to Order Feedback once received.

## Ordering, modification, and stock questions

- All handled by the shared Chatbot Orchestrator (see `02-chatbot.md`) — WhatsApp is one of its four input channels, with no WhatsApp-specific conversational logic beyond the platform integration itself.

## Interactions with other components

- **Chatbot Orchestrator** — for anything conversational (ordering, modifying, asking about stock).
- **Notification service** — for anything proactive (tracking pushes, order confirmation, feedback request).
- **Meta's WhatsApp Cloud API** — the actual message delivery path.

## Edge cases worth knowing about

- **The checkout phone number isn't a valid or reachable WhatsApp number** — the WhatsApp confirmation fails on delivery (Meta reports the failed status back) and it doesn't block the order. With no email in Phase 1 this means the customer gets no confirmation at all, so the failure has to surface in the dashboard's alert inbox for someone to call them.
- **Template approval lead time** — this is an external dependency (days to weeks with Meta), so templates need to be drafted and submitted well before this channel's planned launch phase, not the week before.
- **Message length/variable limits** — WhatsApp templates have real constraints on structure, so order summaries need to stay concise rather than listing an unbounded number of items inline.
