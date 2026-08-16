# 14 — Image recognition

## Purpose

Lets a customer send a photo of a product they want instead of describing it in words. The system's job splits into two very different pieces: understanding what's in the photo (a solved, automatable problem), and deciding whether the brand can produce it if it's not something already sold (a business/craft judgment that stays with staff — see the decision below).

> **Not built in Phase 1.** Until this component exists, an incoming photo goes straight to human handoff with the image attached — the agent is given no image tool and must not describe or guess at the garment. See `AGENTS.md`. Everything below describes the component once it *is* built; don't build any of it early.

## How it works

1. **Image received** — any incoming image on WhatsApp, FB DM, IG DM, or TikTok DM is handed here by the agent rather than answered conversationally (see `02-chatbot.md`).
2. **Analyze the image** — an image-understanding step identifies what's in the photo: garment type, color, and style features. This is a description step, not a matching step yet.
3. **Compare against the catalog** — the photo is compared against every product's `images` (the 177 photos under `data/images/`, see `08-product-database.md`) using image similarity, not just the text description from step 2. A text description alone ("blue jacket") would match too many unrelated products; actual visual similarity is what narrows it down correctly. The photos are held locally rather than fetched from a CDN on every comparison. **`color_images` lets a match land on the right colourway**, not just the right garment — which matters more now that one product covers several colours.
4. **Branch on match confidence:**
   - **Confident match** → treated exactly like a stock question: the bot identifies the product, checks live stock, and can proceed straight into ordering it.
   - **No confident match** → treated as a custom request. The photo, the AI's description, and the conversation thread are sent to the admin dashboard's custom-request queue. The bot tells the customer their request has been received and a team member will follow up — it does not attempt to promise or rule out production itself.

## Why production feasibility stays a staff decision

Whether something can actually be produced depends on fabric sourcing, pattern complexity, and production time — the same judgment a human already makes for custom work today. Having the bot answer "yes we can make this" carries real business risk if it's wrong: overcommitting to something that can't actually be delivered damages trust more than a slightly slower human reply would. This was a deliberate scope decision, not a limitation to route around later.

## Interactions with other components

- **Chatbot Orchestrator** — the entry point; routes any incoming image here instead of processing it as text.
- **Product DB** — supplies `images` and `color_images` for every catalog item to compare against, plus `style` and the variant colours, which give the match a text signal to cross-check the visual one against.
- **Admin dashboard** — receives custom-request queue entries with the photo, the AI's description, and full conversation context attached.
- **Order service** — invoked normally once a confident catalog match leads into an actual order.

## Edge cases worth knowing about

- **Multiple photos of the same item in one message** — treated as one request; the best match across all submitted photos is used rather than evaluating each in isolation.
- **Low-quality or blurry photos** — if the image itself is too unclear to analyze confidently, the bot asks for a clearer photo before attempting a match, rather than guessing from a bad signal.
- **Close-but-not-exact matches** (e.g. same style, different color than anything in the catalog) — the confidence threshold decides which side of the branch this falls on; tuning that threshold is an ongoing calibration, not a one-time setting, since too loose a threshold sends real customers into the "we don't have this" branch, and too tight a threshold routes obvious catalog matches to staff unnecessarily.
- **A customer sends an unrelated photo by mistake** (not clothing at all) — the analysis step should recognize this isn't a garment and respond accordingly rather than forcing it into either branch.
