# Prompt for Claude Code — hardening the Gemini provider

Copy everything below the line.

---

Before this project's chatbot, I built a separate WhatsApp ordering bot on the
same idea (LLM agent + tool calls + Gemini) and shipped it far enough to place
real orders locally. In that build I hit six concrete failure modes in the
Gemini integration, each with a specific fix. I want you to check whether
`chatbot/providers/gemini.py` (or wherever the Gemini provider lives now)
already handles each one, and harden whichever ones it doesn't. Don't rewrite
what already works — this project already has a provider abstraction and DB-backed
sessions, which is further than the old bot started. This is specifically about
the Gemini-specific sharp edges.

## 1. Gemini's newer API keys have a different shape

Newer keys look like `AQ.Ab...` instead of the older `AIzaSy...`. If there's any
validation, logging, or masking logic that assumes the `AIzaSy` prefix, it will
misfire on a valid key. Check for that assumption and remove it — accept
whatever format is in `GEMINI_API_KEY` without pattern-checking it.

## 2. Model aliases can 404 or silently point at a quota-exhausted model

`gemini-2.5-flash` and similar named models get deprecated out from under
existing code, and aliases like `-latest` can resolve to a model whose free
quota is effectively zero, producing `429` errors that look like a bug rather
than a quota problem. Add (or verify there's already) a small utility that
lists models actually available to the configured API key and picks a working
one, rather than hardcoding a model string that can go stale. Surface which
model got picked in a debug log line, since silently switching models is its
own kind of confusing.

## 3. Gemini 3's tool-calling requires replaying `thought_signature`

If the model used is a Gemini 3 variant, every tool-call turn comes back with a
`thought_signature` that has to be stored and sent back on the next turn in the
conversation history, or the API rejects the follow-up with a missing-signature
error. Check whether the `ToolCall` shape in the provider abstraction has a
field for this, whether it survives a round-trip through session storage (the
sessions are DB-backed per `16-supporting-tables.md`), and whether it's
actually replayed on the next request. This is easy to miss because it only
breaks on the *second* tool call in a conversation, not the first — test a
multi-turn tool-calling exchange specifically, not just one call.

## 4. `INVALID_ARGUMENT` from Gemini is often too vague to debug from the message alone

When the API rejects a request, the error message frequently doesn't say which
field was the problem. If there's no way to isolate this currently, add a debug
mode (env-gated, off by default) that logs the exact request payload sent to
Gemini right before the call, so a rejected request can be diagnosed from the
log instead of guessed at.

## 5. Errors must never be swallowed silently, but must never leak to the customer either

Every exception in the chatbot path should be logged (file + stdout is fine)
with enough context to debug, while the customer-facing reply stays generic —
something like "we're having trouble right now, try again in a moment" rather
than a stack trace or raw provider error. If there's a `DEBUG` env flag that
currently exposes raw errors into the reply text for local testing, confirm
it defaults to off and there's no path for it to be left on in a deployed
`.env`.

## 6. Product option ordering isn't consistent across the catalog

This one's from the old bot's *data* layer, not Gemini, but check it's not
present in this project's catalog handling either: in `products.json` from
Shopify, `option1`/`option2` don't reliably mean the same axis (size) across
every product — some products have size in `option1`, others in `option2`,
whichever the source data happened to use. Since this project already went
through `merge_catalog.py` with an explicit `classify_options()` step, this is
probably already handled — just confirm it's resolved by inspecting known
size values rather than by position, and isn't something that could regress if
the catalog gets re-merged later.

## What I want back

For each of the six, tell me: already handled / not present / partially
handled, and what you changed. Add a test for #3 specifically (multi-turn tool
call with signature replay) since it's the one most likely to pass locally on
a single call and fail in real use. Don't touch anything outside the Gemini
provider and its tests unless a fix genuinely requires it — if it does, say why
before doing it.

---

## Why this prompt looks like this

Six items, not "review the provider for issues" — a vague ask gets a shallow
pass. Each one names the exact symptom you'd see if it's wrong, so if Claude
Code says "handled" you can verify against the described failure mode.

Item 3 gets the most weight because it's the one that's invisible until a
second tool call happens — everything else fails loud and fast; this one fails
quiet and late.

Asking for a report before assuming a rewrite matters because this project's
provider layer is already more mature than the old bot's (DB sessions, real
abstraction) — the risk is Claude Code "fixing" something that's already fine
and destabilizing it in the process.
