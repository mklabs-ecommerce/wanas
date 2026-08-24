# Voice notes and photos

What happens when a customer sends something that is not text, and why it is
built the way it is.

## Why this changed

Both used to end the conversation. A photo or a voice note was queued for a
person and the bot went silent.

That was the right call while the model could neither see nor hear — a guess
about a garment the shop may not make is worse than a handoff. It stopped being
the right call for two reasons:

- **Voice notes are not an edge case in Egypt.** A large share of WhatsApp
  traffic is spoken. "Someone will get back to you" was the entire conversation
  for those customers.
- **A photo is usually a question**: "do you have this?" Answering it needs the
  model to look — but nothing here lets it *answer* from looking.

## Voice notes

```
audio message → download (in the request, before the debounce window)
              → provider.transcribe(bytes, mime)
              → the transcript is the message
              → ordinary agent turn, every guardrail unchanged
```

The transcript is asked for verbatim: no summary, no translation, no answering
what was said. Egyptian Arabic stays in Arabic script and an English product
name stays in Latin, because that is the mixture the prompt is written for and
the mixture `search_terms` expects.

The stored history keeps a `[رسالة صوتية: …]` tag so staff reading a thread can
see it was spoken. The bot is told not to mention it — "I listened to your
message" is not how a shop assistant replies to someone who just spoke to them.

**Falls back to a person** when: `VOICE_NOTES_ENABLED=0`, the provider cannot
listen (`supports_audio` is false — the rehearsal stand-in, for instance), the
file never downloaded, it is bigger than 12 MB, its extension is unrecognised,
the provider errored, or the transcript came back empty. The handoff reason is
`voice_received`, its own value rather than `out_of_scope`, because staff
working the queue need to see that a message is waiting on someone to listen.

## Photos

```
image message → download
              → shortlist built from the real catalog (id, name, category, colours)
              → provider.inspect_image(bytes, mime, catalog=shortlist)
              → ImageReading{product_id?, confidence, description, is_garment}
              → a note handed to the agent, never an answer
```

`ImageReading` is deliberately narrow. The model may point at a product **from
the list it was given**, or say it recognised nothing. It may not name a
garment the shop might sell, invent a colour, or quote anything.

Two guarantees sit on top of it:

1. **A `product_id` that is not on the shortlist is discarded** in the provider,
   before the caller sees it, and logged. A confident match on something the
   shop does not stock is the exact failure this exists to prevent.
2. **The note carries the product's name, not its id.** A name is something the
   customer may safely read; an id echoed into a WhatsApp reply is a leak.

### What the agent is told

| Reading | The note says | What the bot does |
| --- | --- | --- |
| Match above `IMAGE_MATCH_CONFIDENCE` | "the closest product we have is *WANAS Hoodie*; verify with the tools" | Confirms it is what they meant, then looks up price and sizes properly |
| Match below the threshold, or none | the short description, and *do not claim we have one* | Asks a short clarifying question, or offers the nearest real thing and says so |
| `is_garment: false` | "this is not a garment" | Does not try to sell; a complaint goes to a person |

The threshold is configuration, not a constant, because it is the dial between
"the bot guesses" and "the bot asks" — and which side a shop wants depends on
how distinctive its catalog is.

**Falls back to a person** on the same conditions as voice, with reason
`image_received`.

Either fallback shows up at `/dashboard` — see "The staff dashboard" in
`docs/ARCHITECTURE.md` — with the actual audio/image paths in its payload, so
staff working the queue know a `voice_received` item is waiting on a listen
and an `image_received` one on a look, not just "reply to this person".

## The invariant that survives all of it

A vision reading is a hint about *which tool to call*. It is never a fact.
Nothing in this path can produce a price, a size, or an "in stock" — those come
from `get_products` / `get_variants` exactly as they do for a typed message.

`tests/test_media.py` pins this down: the shortlist comes from the database, an
unknown `product_id` is not a match, a low-confidence reading never names the
product, and every fallback still reaches a human.

## Cost

One extra model call per voice note or photo, on the conversation model
(`LLM_MEDIA_MODEL` overrides it only under `LLM_PROVIDER=gemini`; OpenRouter
runs both on its one shared model). It runs on the worker thread, so it does
not hold the webhook. `MAX_MEDIA_BYTES` (12 MB) refuses anything that is
obviously not a phone photo or a voice note before it is uploaded.
