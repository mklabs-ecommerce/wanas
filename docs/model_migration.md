# Model migration: GLM + Gemini -> GPT Luna 6 (2026-10-02)

## Checkpoint

- Git tag **`pre-luna-checkpoint`** (annotated, pushed) on `aaea470`, the
  `main` this migration started from.
- Railway deployment id: **not recorded**. The `RAILWAY_TOKEN` supplied for
  this migration was rejected by the Railway CLI ("Invalid RAILWAY_TOKEN"), so
  neither the live deployment id nor the live variable values could be read.
  Read them from the Railway dashboard (Deployments tab) before switching.
- Model variables before the switch, as the local `.env` and `.env.example`
  set them (production is expected to match; confirm in Railway):

  | variable | value before |
  |---|---|
  | `LLM_PROVIDER` | `openrouter` |
  | `LLM_MODEL` | `z-ai/glm-5.3-flash` |
  | `LLM_MEDIA_MODEL` | `google/gemini-3.1-flash-lite` |
  | `LLM_AUDIO_MODEL` | *(did not exist)* |
  | `OPENROUTER_PROVIDERS` | `z-ai,deepinfra,novita` |
  | `OPENROUTER_QUANTIZATIONS` | `fp8,bf16,fp16` |
  | `OPENROUTER_REASONING_EFFORT` | `medium` |
  | `COMMENT_CLASSIFIER_MODEL` | blank (reuses `LLM_MODEL`) |

## Variables after the switch

| variable | value |
|---|---|
| `LLM_MODEL` | `openai/gpt-6-luna` |
| `LLM_MEDIA_MODEL` | `openai/gpt-6-luna` (photos) |
| `LLM_AUDIO_MODEL` | `google/gemini-3.1-flash-lite` (voice notes) |
| `OPENROUTER_PROVIDERS` | *blank* |
| `OPENROUTER_QUANTIZATIONS` | *blank* |
| `OPENROUTER_REASONING_EFFORT` | `medium` (unchanged) |

Why each one:

- **Voice stays on Gemini.** OpenRouter lists `openai/gpt-6-luna` as
  `text+image+file->text`, with no audio input, so sending it a voice note
  returns a 400. `LLM_AUDIO_MODEL` is new: it splits voice from photos, and
  leaving it blank keeps the old behaviour (`LLM_MEDIA_MODEL` hears too).
- **Provider pins blanked.** The z-ai/deepinfra/novita order and the fp8+
  quantization filter were chosen for GLM. Applied to an OpenAI id they
  filter out every upstream, and OpenRouter also turns sticky (cache) routing
  off whenever `provider.order` is set.
- **No `temperature`.** Luna's `supported_parameters` has no `temperature`.
  `openrouter._post` now drops it for the GPT-5/6/o-series
  (`accepts_temperature`). Otherwise `require_parameters` would exclude the
  only host.

## Rollback

Model-only rollback (seconds, no deploy of code):

```
railway variables --set LLM_MODEL=z-ai/glm-5.3-flash \
  --set LLM_MEDIA_MODEL=google/gemini-3.1-flash-lite \
  --set LLM_AUDIO_MODEL= \
  --set OPENROUTER_PROVIDERS=z-ai,deepinfra,novita \
  --set OPENROUTER_QUANTIZATIONS=fp8,bf16,fp16
```

Setting variables triggers a redeploy; watch it with `railway logs`. The code
changes are backwards-compatible with GLM: the temperature drop applies only
to OpenAI ids, the blank-enum option is harmless, and the cache key is just
an extra field OpenRouter uses for sticky routing.

Full rollback (code too):

```
git revert --no-edit <migration commit>..HEAD   # or: git revert <sha>
git push origin main
```

then the variable restore above. Last resort:
`git reset --hard pre-luna-checkpoint` on a branch and redeploy that branch
from Railway's dashboard ("Deploy" on a commit). Never force-push `main`.

## What changed in code

- `assistant/providers/openrouter.py`
  - drops `temperature` for models that refuse it;
  - sends `prompt_cache_key` and `session_id` per conversation
    (`rehla-<channel>-<telemetry hash>`, never the phone number). This is
    OpenAI's cache key, and OpenRouter's sticky routing reads the same value;
  - offers a blank option on every optional enum in tool schemas and drops
    blank/null arguments. GPT models fill in every parameter they are shown:
    Luna searched `sleeve: "sleeveless"`, then `"half"`, then `"long"` for a
    customer who named no sleeve, and never found her product;
  - `transcribe` uses `LLM_AUDIO_MODEL`.
- `common/telemetry.py`: the per-turn log line now carries `prompt_tokens`
  and `cached_tokens` totals. This is how to confirm in production that
  caching is hitting.
- `assistant/prompt.py`: tuned for Luna, which follows rules more literally
  than GLM did:
  - search by the product's name with no filters the customer didn't give;
  - every detail the customer gave is kept, «زي ما قلتلك» included;
  - a governorate stated outright beats a district in the address;
  - a street address is enough, so the order is not blocked on a building
    number;
  - «متاح مقاسات إيه؟» means stock (`get_variants`), not the size chart;
  - «ضفت» is said only after `add_to_cart` ran in the same turn.
- The prefix that caching depends on was already stable: the system prompt is
  static per channel, and the per-turn extras (`system_extra`) are appended at
  the end.

## Benchmark

`scripts/rehla/benchmark.py`, 14 scenarios, end to end on the local shelf.
Results are in `benchmarks/`. Comparison:

Each cell shows result · scenario seconds · USD. "Old code" means the tree at
`pre-luna-checkpoint`. "Final code" means after this migration. The code fixes
below improve GLM too, so the middle column separates the model's effect from
the code's.

| scenario | GLM baseline (old code) | GLM (final code) | GPT Luna 6 (final) |
|---|---|---|---|
| greeting_browse | PASS · 14s · $0.0018 | PASS · 20s · $0.0054 | PASS · 18s · $0.0024 |
| arabic_search | PASS · 11s · $0.0011 | PASS · 13s · $0.0011 | PASS · 14s · $0.0020 |
| image_match | PASS · 17s · $0.0022 | PASS · 18s · $0.0017 | PASS · 18s · $0.0007 |
| voice_note | PASS · 12s · $0.0012 | PASS · 14s · $0.0011 | PASS · 16s · $0.0009 |
| order_cairo_70 | PASS · 48s · $0.0071 | PASS · 81s · $0.0081 | PASS · 49s · $0.0032 |
| order_alex_85 | PASS · 38s · $0.0054 | PASS · 53s · $0.0049 | PASS · 33s · $0.0024 |
| exchange_return | PASS · 4s · $0.0005 | PASS · 7s · $0.0005 | PASS · 4s · $0.0003 |
| size_help | PASS · 23s · $0.0026 | PASS · 29s · $0.0027 | PASS · 14s · $0.0010 |
| unsold_garment | PASS · 11s · $0.0011 | PASS · 28s · $0.0024 | PASS · 7s · $0.0004 |
| drift_then_return | FAIL · 62s · $0.0081 | PASS · 56s · $0.0052 | PASS · 34s · $0.0025 |
| multi_item_cart_edit | PASS · 71s · $0.0113 | PASS · 102s · $0.0149 | PASS · 57s · $0.0048 |
| angry_customer | PASS · 6s · $0.0005 | PASS · 4s · $0.0005 | PASS · 2s · $0.0002 |
| ambiguous_short | PASS · 8s · $0.0009 | PASS · 10s · $0.0010 | PASS · 7s · $0.0003 |
| long_memory | FAIL · 156s · $0.0338 | FAIL · 175s · $0.0334 | PASS · 199s · $0.0426 |

| total | GLM baseline (old code) | GLM (final code) | GPT Luna 6 (final) |
|---|---|---|---|
| passed | 12/14 | 13/14 | 14/14 |
| wall seconds | 482 | 609 | 471 |
| median scenario s | 17.5 | 27.6 | 18.0 |
| prompt tokens | 1,813,372 | 1,871,756 | 1,819,584 |
| completion tokens | 6,420 | 6,506 | 16,716 |
| cached share | 91% | 90% | 82% |
| cost USD | $0.0776 | $0.0831 | $0.0638 |

Notes:

- Luna is **>= baseline on every scenario**: 14/14 against 12/14. It is
  cheaper overall ($0.064 against $0.078 for the suite) and about as fast end
  to end. It writes more completion tokens (reasoning) but needs fewer hops.
- Cached share is lower for Luna (82%) than for GLM. OpenAI caches
  automatically in 128-token blocks after the first 1024 tokens, and a
  conversation's first call is always a miss. Production log lines now carry
  `prompt_tokens` / `cached_tokens` per turn to watch this.
- The baseline's `long_memory` customer gave a first name only («ريم»). The
  final runs use «ريم حسن», because `assistant/customer_name.py` asks for a
  full name when only a first name is known. That is an existing shop rule:
  GLM ignored it, and Luna follows it.
- Fixes found by the benchmark that are not about the model, and help GLM
  too:
  - «أكدي» on its own was not accepted as a yes by `confirm_order`;
  - `ask_governorate` ignored a governorate typed instead of tapped once a
    region was chosen;
  - a district in the address (الدقي, Giza) reopened a governorate the
    customer had already stated (القاهرة).
- One live run is a sample, not a distribution. Cairo failed in 2 of the 6
  Luna runs before the governorate fixes, and passed after them.

Benchmark command (local shelf, no Shopify or Meta, real OpenRouter):

```
python scripts/rehla/benchmark.py --label <name>
```

