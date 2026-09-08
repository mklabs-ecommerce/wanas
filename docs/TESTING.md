# Testing

**1694 tests, 22 skipped**, across 79 test modules. Flat layout — one
`test_<subject>.py` per subject rather than a mirror of the source tree,
because what is worth proving does not map one-to-one onto where the code
lives.

---

## Running them

```bash
make check                                       # ruff + the full suite (what CI runs)
make test                                        # the suite alone
make lint                                        # ruff check ., no autofix

pytest tests/test_order_transaction.py           # one file
pytest tests/test_order_transaction.py::test_name # one test
pytest tests/ -k "instagram and comment"         # by name
```

### Against PostgreSQL — do this before deploying

```bash
WANAS_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/wanas make test
```

This is not optional diligence, it is a lesson already paid for. SQLite
overloads `max()` as a two-argument scalar; PostgreSQL's `max()` is
aggregate-only, so `max(a, b)` is `UndefinedFunction` there, **every time,
unconditionally**. It passed every test on the SQLite default and broke *every
single order placement* in production, at the last step, after the remote
order had already been created.

`WANAS_TEST_DATABASE_URL` is the only way to aim the suite at PostgreSQL. An
ambient `DATABASE_URL` is deliberately **ignored** — the fixtures drop entire
schemas, so nothing ambient may decide what gets dropped.

### Live tests (cost real quota)

```bash
RUN_LIVE_TESTS=1 pytest tests/test_conversation_live.py -v
```

---

## How the suite is isolated

`tests/conftest.py`:

- forces `DATABASE_URL` to the suite's own throwaway SQLite file (assigned,
  never `setdefault`);
- **blanks** real `LLM_API_KEY` / `SHOPIFY_*` / `WHATSAPP_*` before
  `config.settings` is imported anywhere, so the suite can never talk to a real
  model, a real store, or a real phone number — and so the same test does not
  pass on one machine and fail on another;
- pins `LLM_PROVIDER=fake` and `MESSAGE_DEBOUNCE_SECONDS=0`, so a webhook test
  can assert on the reply on the line after the request;
- drops and recreates the whole schema per test;
- installs an in-memory fake commerce shelf (`tests/fake_shopify.py`, 1295
  lines) seeded from the catalogue rows.

CI blanks the same variables again at the job level — belt and braces, because
a runner with any of them set must not be able to reach anything real.

### Markers

| Marker | Effect |
|---|---|
| `no_shopify` | skip installing the fake shelf, to exercise the "platform unreachable" path |
| `live` | hits a real model and costs quota; skipped unless `RUN_LIVE_TESTS=1` |

`--strict-markers` is on, so a typo in a marker fails rather than silently
matching nothing.

---

## CI

Three jobs on every push and pull request:

| Job | What it proves |
|---|---|
| `check (3.11)` | ruff clean and the suite green on the floor `pyproject.toml` declares |
| `check (3.13)` | the same **on the interpreter production actually runs** |
| `postgres` | the suite against real PostgreSQL 16, on 3.13 |

The two-version matrix exists for the same reason `requirements.txt` is
pinned: the build that ships must be the build that was tested.

---

## What is covered, by area

| Area | Modules |
|---|---|
| Agent, session, memory | `test_agent_and_session`, `test_conversation_memory`, `test_conversation_behavior`, `test_display`, `test_turn_retry` |
| Tool contracts | `test_tool_contracts` (1120 lines — every tool's refusals) |
| Providers | `test_openrouter_provider` (1096), `test_gemini_provider`, `test_media` |
| Order flow | `test_order_transaction`, `test_order_completion`, `test_shopify_full_orders`, `test_shopify_order_writes` |
| Inventory / re-engagement | `test_reengagement`, `test_conversation_abandonment`, `test_catalog_sync` |
| WhatsApp | `test_whatsapp_channel`, `test_whatsapp_bsuid`, `test_read_receipts`, `test_outbound_delivery` |
| Instagram | `test_instagram_comments` (1276), `test_instagram_channel`, `test_instagram_client`, `test_instagram_token`, `test_instagram_usernames`, `test_instagram_orders`, `test_instagram_prompt` |
| Shopify integration | `test_shopify_admin_products` (835), `test_shopify_webhooks`, `test_shopify_product_import`, `test_shopify_product_reconcile`, `test_shopify_live_catalog`, `test_shopify_webhook_registration` |
| Dashboard | 13 `test_dashboard_*` modules + `test_staff_permissions` |
| Data integrity | `test_schema_drift`, `test_bug2_durability`, `test_session_retention`, `test_seed_import`, `test_startup_seed` |
| Security surfaces | `test_public_media`, `test_staff_permissions`, `test_dashboard` (login/cookie), `test_legal` |
| Resilience | `test_bug1_resilience`, `test_dispatcher`, `test_retention` |
| Infrastructure | `test_logging_streams`, `test_timeutil`, `test_bidi` |
| Localisation | `test_dashboard_i18n` — **adding a dashboard screen without its translations fails a test** |

---

## The flows that must always be verified

If you change anything near these, run the named module *and* think about
whether it still proves what its name claims.

### 1. Placing an order — `test_order_transaction.py`, `test_order_completion.py`

The compensating transaction. Both end states must hold: the order recorded
and committed, **or** the remote order cancelled. Specifically:

- a local failure after `orderCreate` cancels the remote order;
- a rollback goes to the **savepoint**, so the cart survives and the customer
  can simply try again;
- notifications failing does not fail the order;
- stock is decremented **once** (by the platform), never twice;
- a second `confirm_order` on an empty cart answers `already_confirmed`, not
  `cart_empty`.

**Run this one on PostgreSQL.**

### 2. Webhook authentication — `test_whatsapp_channel.py`, `test_instagram_channel.py`, `test_shopify_webhooks.py`

- unsigned is refused;
- a forged signature is refused;
- **one channel's app secret does not verify the other's** — they are
  different strings even inside the same app;
- with no secret configured, everything is refused (not accepted);
- a correctly signed delivery for a different shop is refused.

### 3. Idempotency — `test_bug1_resilience.py`, `test_retention.py`

A retried delivery must not produce a second order or a second reply; a
*crashed* turn must release its claim so the retry gets through; and a claim
inside its retry window must survive the retention pass.

### 4. Conversation memory — `test_conversation_memory.py`

The verbatim window never opens on an orphaned `tool_results`; compaction only
ever removes whole messages; alignment only ever keeps more, never less.

### 5. Inbound visibility — `test_inbound_visibility.py`

A message is in the transcript on arrival, not on reply — including when the
turn crashes, when the conversation is paused, and when the model never
answers.

### 6. Deliverability — `test_outbound_delivery.py`, `test_notifications_channels.py`

Outside the 24-hour window with no template, the line is stored
`delivered=False` **and** a staff alert is raised. A stored message is never
recorded as delivered on the strength of having been composed.

### 7. Instagram's public surface — `test_instagram_comments.py`

The bot never answers itself; exactly one private reply per comment, ever; and
every public line comes from a lookup, never from a model.

### 8. Permissions — `test_staff_permissions.py`

Every dashboard route refuses an account without its permission. The sidebar
hiding a nav item is a courtesy; the route refusal is the control.

### 9. Data durability — `test_bug2_durability.py`, `test_session_retention.py`

SQLite in a deployment is refused. A conversation *ending* deletes nothing.

---

## Testing seams

Rather than mocking broadly, the code exposes named seams:

| Seam | Lets a test |
|---|---|
| `assistant/agent.py::_sleep` | prove the retry path without sleeping 30s |
| `LLM_PROVIDER=fake` | script exact model replies (`providers/fake.py`) |
| `tests/fake_shopify.py` | run the whole order flow against an in-memory shelf |
| `tests/fake_instagram.py` | assert on what would have been sent |
| `app.py::build_log_handlers` | assert log routing without mutating the root logger |
| `MESSAGE_DEBOUNCE_SECONDS=0` | run a turn inline in the caller's thread |
| `retention.prune_webhook_events(older_than_days=…)` | state the boundary rather than reach into settings |

That last pattern is the house style: a cap or window is an **argument with a
settings default**, so a test can state the boundary it is about.

---

## Beyond the suite: what still needs a person

Automated tests cannot cover these. They are listed in
[PRODUCTION_AUDIT.md](../PRODUCTION_AUDIT.md) as requiring manual
verification.

- A real message from a real customer account on each channel, end to end.
- A real order placed and then fulfilled in the platform's admin, to prove the
  status push actually reaches the phone.
- A proactive message *outside* the 24-hour window, which needs an approved
  template and cannot be simulated.
- A real voice note and a real photo from a phone (codecs and sizes differ
  from fixtures).
- An owner alert actually arriving in the inbox.
- The dashboard's rendering, RTL/LTR layout, and behaviour on a phone.
