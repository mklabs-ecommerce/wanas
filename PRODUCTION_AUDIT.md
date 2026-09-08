# Production audit

**Date:** 2026-09-08
**Repository:** `mklabs-ecommerce/wanas` @ `01274c3` (main, at audit start)
**Production:** Railway project `imaginative-sparkle`, service `wanas`,
environment `production`, `wanas-production-381d.up.railway.app`
**Verdict:** **PASS with two items requiring action** — one of them outside
this repository.

---

## 1. Scope

A full audit of the deployed system: application architecture, the AI agent
and its prompt, tools and tool calling, the Shopify / WhatsApp / Instagram
integrations, the database and its consistency rules, sessions and history,
cart and order flows, inventory, error handling and retries, authentication
and security, configuration, logging and monitoring, performance and
reliability, background jobs, deployment configuration, dependencies, dead
code and duplicated logic, and technical debt.

Production was inspected directly — GitHub and Railway APIs plus live HTTP
probes — rather than assumed to match the local checkout.

**~65,000 lines** across 8 layers, 22 database tables, 19 agent tools,
4 external vendors, 78 test modules.

---

## 2. Skills used

| Skill | Source | Used for |
|---|---|---|
| `find-skills` | `vercel-labs/skills` | discovering the rest |
| `fastapi` | `fastapi/fastapi` (official) | FastAPI review reference |
| `use-railway` | `railwayapp/railway-skills` (official) | Railway deployment/API inspection |

`pip-audit` was installed and run for dependency CVEs.

Searched-and-rejected: several third-party "security-audit" skills, all with
low install counts and unverified provenance. Given this repository holds live
commerce credentials, running unvetted third-party agent code against it was
not a good trade; the audit used first-party tooling and direct inspection
instead.

---

## 3. Checks performed

| Check | Result |
|---|---|
| `ruff check .` (project scope) | **PASS** — clean |
| Extended lint (F, S, B, PERF, RUF, BLE, TRY, DTZ, SLF, ERA) | **PASS** — no unused imports, no unused variables, no hardcoded SQL, no naive-datetime bugs; remainder is documented house style |
| Full test suite, Python 3.12 local | **PASS** — 1694 passed, 22 skipped |
| CI `check (3.11)` | **PASS** |
| CI `check (3.13)` — *the deployed interpreter, never tested before* | **PASS** |
| CI `postgres` (PostgreSQL 16) | **PASS** — the pre-deploy check `CLAUDE.md` requires |
| `pip-audit` on pinned runtime deps | **PASS** — no known vulnerabilities |
| Secret scan: tracked files + full git history | **PASS** — `.env` never committed; no credential-shaped strings |
| Independent end-to-end surface probe (22 assertions, written for this audit) | **PASS** — 22/22 |
| Live production HTTP probe | **PASS** — see §5 |
| Env-var cross-check: `settings.py` ↔ `.env.example` ↔ production | **PASS** — no orphans; 8 documented aliases noted |
| Python 3.13-removed stdlib modules (PEP 594) | **PASS** — none used |

### The independent surface probe

Written specifically for this audit rather than reusing the project's own
tests, so a shared wrong assumption could not hide a hole. It boots the real
app and probes it as an outsider — 22/22 pass:

- `/health` 200, catalog seeded, every integration flag correct
- WhatsApp webhook: unsigned **403**, forged signature **403**, valid **200**
- **Instagram rejects the WhatsApp app secret** and accepts only its own
- Shopify webhook: unsigned **401**; correctly signed but for another shop **403**
- verify handshake: wrong token **403**, correct token echoes the challenge
- all 8 dashboard API routes refuse an anonymous caller
- bad credentials → 401 with no cookie; forged session cookie → 401
- public media: bad token **404**; **`data/inbound` refused even under a
  correctly computed token**
- `/harness` **404** with the flag off; `/privacy` served

---

## 4. Critical customer flows verified

| Flow | How | Status |
|---|---|---|
| Inbound message → reply | code trace + suite + live log evidence of real traffic | **PASS** |
| Webhook authentication, all 3 vendors | independent probe, local **and** live | **PASS** |
| Idempotency / retry safety | code trace + `test_bug1_resilience` + new retention tests | **PASS** |
| Order placement + compensating cancel | code trace + `test_order_transaction` on PostgreSQL in CI | **PASS** |
| Inventory (single decrement, live reads) | code trace + suite | **PASS** |
| Order status push | code trace + `test_shopify_webhooks` | **PASS** (delivery outside the 24h window: see WARNING-1) |
| Conversation memory and compaction | code trace + `test_conversation_memory` | **PASS** |
| Inbound visibility before reply | code trace + `test_inbound_visibility` | **PASS** |
| Instagram comments (live public surface) | code trace — every public line is a lookup, never model text | **PASS** |
| Dashboard auth + per-section permissions | independent probe + `test_staff_permissions` | **PASS** |
| Scheduled jobs | code trace + new scheduler test | **PASS** |
| Real message on a real handset | — | **REQUIRES MANUAL VERIFICATION** |

---

## 5. Production deployment inspected

### GitHub — **PASS**

| Item | Finding |
|---|---|
| Repository | `mklabs-ecommerce/wanas`, default branch `main` |
| Latest commit on main | `01274c3` (2026-09-07T19:39:20Z) |
| Local worktree | identical to `origin/main` — **no uncommitted or missing changes** |
| CI history | 121 runs; last 10 all green |
| Open issues / PRs | none at audit start |

### Railway — **PASS**

| Item | Finding |
|---|---|
| Project / service | `imaginative-sparkle` / `wanas` (+ `Postgres`) |
| Deployed commit | **`01274c3` — exactly the main tip.** Production is running the expected code. |
| Status | `SUCCESS`, deployed 2026-09-07T19:39:22Z |
| Start command | `uvicorn app:app --host 0.0.0.0 --port $PORT` — correct |
| Builder | Railpack, Python **3.13.15** |
| Restart policy | `ON_FAILURE`, max 10 |
| Replicas | 1 (matches the in-process debouncer/scheduler design) |
| Failed deployments | **none** in the last 25 |
| Crash / restart loops | **none** — one clean boot, no restarts since |
| Runtime errors in logs | **none.** The only warnings present were this audit's own signature probes. |

### Live health — **PASS**

```
status ok · llm openrouter (key set) · whatsapp configured + webhooks configured
instagram configured + webhooks configured · comments on · token valid to 2026-10-31
shopify configured + webhooks configured · voice on · vision on
dashboard configured · alerts via resend · catalog 19 products / 211 variants
```

Live security probe: `/` 307→`/health`; WhatsApp **403**, Instagram **403**,
Shopify **401** unsigned; `/harness` **404**; `/dashboard/api/*` **401**;
`/privacy` **200**. Those three refusals appeared in the production log as
`rejected a webhook with a bad signature` — **signature verification confirmed
working in production, live.**

### Production configuration — **PASS**

50 variables. Every required credential set. Critically:

- `CHATBOT_DEBUG=0` ✓
- `HARNESS_ENABLED=0` ✓
- `DATABASE_URL` → PostgreSQL ✓
- all three webhook secrets set ✓
- `DASHBOARD_SESSION_SECRET` set (64 chars) ✓

**No secret value was read, printed, logged, or written to any file during
this audit.** Only names, lengths, and non-secret boolean/enum values were
inspected.

---

## 6. Issues found and fixed

All five are in **[PR #3](https://github.com/mklabs-ecommerce/wanas/pull/3)**,
branch `fix/production-audit-hardening`, with 13 new tests. All three CI jobs
green.

### FIXED-1 — `webhook_events` grew without bound (reliability)

One row per inbound WhatsApp message, per Instagram message *and* comment, and
per Shopify delivery. The only `DELETE` anywhere was `release_claims`, which
fires only when a turn has already failed. So the table held one row per
message the shop had **ever** received, forever, on the same small Postgres as
the orders. Slow, invisible, and only noticed once already large.

The `received_at` index had been on the model since it was written and was
read by nothing — the index a retention pass needs, and fairly clearly the one
it was put there for.

**Fix:** `domain/services/retention.py`, pruning on the existing scheduler
tick. Safe because the row's job is bounded in time — it refuses a platform
*retry*, and Meta gives up after days while Shopify stops at 48 hours. Default
30 days (`WEBHOOK_EVENT_RETENTION_DAYS`) is an order of magnitude of headroom;
pruning *earlier* than a platform still retries would re-open the
duplicate-order door, which is why the default is generous rather than tight.
6 new tests, including one proving a kept claim still suppresses a duplicate.

### FIXED-2 — every log line was filed as `error` (monitoring)

Railway files a log line's severity by the stream it arrived on, and
`logging.basicConfig` writes only to stderr. So **filtering the deploy's logs
for errors returned the whole log** — the same as having no filter. Observed
directly: `INFO wanas: schema matches the models` arrives with severity
`error`.

**Fix:** INFO and below → stdout, WARNING and above → stderr, so "something is
in stderr" means what it says. Split rather than moved wholesale, or the
filter is broken the other way. uvicorn's access log was already on stdout;
one process now describes itself one way. 5 new tests.

### FIXED-3 — the login answered "does this username exist?" (security)

The response body was already identical for a wrong password, an unknown
username and a deactivated account. The **timing** was not: an unknown name
returned before any hashing, a real one after 240,000 rounds of PBKDF2. Tens
of milliseconds is comfortably measurable over the internet, and a username
oracle is the first half of a targeted attack on a dashboard that reads every
customer's address and order history.

**Fix:** `authenticate` verifies against a real dummy digest when there is no
account, so all three answers cost the same. A genuine hash, not a sleep —
nothing to re-tune if the iteration count changes. 2 new tests.

### FIXED-4 — the version that shipped was not the version that was tested (reliability)

Railpack resolves Python from `requires-python = ">=3.11"` and had settled on
**3.13.15**. CI tested **3.11 only**, and nothing pinned either — so a builder
release could have moved production onto a new interpreter with no diff
anywhere in the repository. This is exactly the risk `requirements.txt` is
pinned to avoid, left open one layer down.

**Fix:** `.python-version` pins 3.13 (what production already runs — no
behaviour change today) and CI runs the matrix 3.11 + 3.13; the PostgreSQL job
moves to 3.13. **The deployed interpreter now passes the suite for the first
time.**

### FIXED-5 — no healthcheck configured (deployment)

`healthcheckPath` was null, so traffic switched as soon as the process was
**listening** — and listening is not working. This app does substantial work
at startup, and a container that bound the port then failed any of it would
have been sent live customer messages while the healthy one it replaced was
already gone. The restart policy catches a process that *dies*; nothing caught
one that came up *wrong*.

**Fix:** `railway.toml` points the healthcheck at `/health` (which opens a DB
session and counts the catalog, so it answers 200 only when the process can
reach Postgres and has something to sell), 300s timeout. Deliberately partial
— the host merges only the keys named, and the start command is **not**
restated, because restating it is the one way that file could silently change
it.

---

## 7. Production issues requiring action

### ACTION-1 — an abandoned deployment holds live store credentials — **FAIL**

**This is the most serious finding in the audit, and it is outside this
repository.**

A second Railway project, `confident-success` / service `backend`, is **still
live and serving** at `backend-production-aa64e.up.railway.app`. It runs the
**superseded** architecture from a different repository
(`mklabs-ecommerce/Wanas_chatbot_F`) — the React storefront and server-rendered
admin that `CLAUDE.md` records as removed — last deployed 2026-08-25.

Verified by direct probe:

- `/health` returns **200** and self-reports `store: p0hd05-m5.myshopify.com`
  — **the same live store the production bot sells from**
- **`/docs` and `/openapi.json` are publicly readable (200)** — a complete,
  unauthenticated map of its API surface
- its environment holds **35 variables including `SHOPIFY_ACCESS_TOKEN`,
  `GEMINI_API_KEY`, SMTP credentials, `CHAT_SESSION_SECRET`, and
  `ADMIN_OWNER_USERNAME` / `ADMIN_OWNER_PASSWORD`**

Why it matters: that abandoned codebase holds **write access to the live
store**. It is not in the current repository, gets no security review, no
dependency updates, and nobody reads its logs. Its own history records a fixed
IDOR on `/chat` that leaked another customer's transcript, name, phone,
address and order numbers — which is the class of bug that goes unnoticed in
an unwatched deployment.

Mitigating: it has **no** WhatsApp or Instagram credentials, so it is not
receiving customer traffic and there is no risk of two bots answering.

**Recommended, in order:**
1. Confirm nothing still depends on it (the storefront moved to a Shopify theme).
2. Delete the Railway service, or at minimum remove its public domain.
3. **Rotate `SHOPIFY_ACCESS_TOKEN` and `GEMINI_API_KEY`** — they have been
   sitting in an unmaintained deployment, and rotation is cheap.
4. Archive `Wanas_chatbot_F` on GitHub so it cannot be redeployed by accident.

Not actioned during this audit: deleting a deployment and rotating live
production credentials are destructive, owner-level decisions.

### WARNING-1 — no approved message templates are configured

None of the five `WHATSAPP_TEMPLATE_*` variables is set in production.
Consequence: **every automated message that falls outside Meta's 24-hour
customer-service window cannot be delivered at all** — the line is stored
`delivered=False` and a staff alert is raised.

The system handles this correctly and honestly; the gap is operational, not a
bug. The one that matters most is `ORDER_UPDATE`: a customer orders, stops
writing, and the parcel ships a day or two later — outside the window by
construction, so the shipping notification silently becomes a staff task.

`docs/WHATSAPP_TEMPLATES.md` already contains the copy to submit, including
the constraint that shaped it (`send_template` posts no `components`, so a
template carrying `{{1}}` passes review and fails at send time). Submit them.

### WARNING-2 — the repository is public

`mklabs-ecommerce/wanas` is public. No secrets are committed and the history
is clean — verified — so this is not an exposure today. But it publishes the
exact security architecture, including which environment variable's absence
disables which control. Worth a deliberate decision rather than a default.

### WARNING-3 — no rate limiting on the dashboard login

PBKDF2 at 240k rounds makes each attempt expensive, and FIXED-3 closes the
username oracle, so this is not urgent. But there is no lockout, no
per-IP throttle, and no failed-login alert on an internet-facing login that
reads every customer's personal data. Worth adding before the staff count
grows.

---

## 8. Security findings

| # | Finding | Severity | Status |
|---|---|---|---|
| S1 | Abandoned deployment with live store write credentials and a public `/docs` | **High** | **ACTION REQUIRED** (§7) |
| S2 | Login username-enumeration timing oracle | Medium | **FIXED** |
| S3 | No rate limiting / lockout on dashboard login | Medium | **OPEN** — recommended |
| S4 | Public repository publishing the security model | Low | **OPEN** — decision |
| S5 | Verify-token compared with `==` rather than a constant-time compare | Informational | **ACCEPTED** — reachable only on the subscription handshake, guarded against the empty-token case, and not a signing key |

### Verified correct

- **All three webhooks refuse when their secret is absent.** No
  `if secret and not verify(...)` anywhere. Confirmed live in production.
- **The two Meta app secrets are genuinely separate.** Probed: Instagram
  rejects a payload signed with the WhatsApp secret.
- Signature comparisons use `hmac.compare_digest`; passwords use PBKDF2-HMAC-SHA256
  at 240k rounds with a per-user salt and a constant-time compare.
- Session cookie: HMAC-signed, `httponly`, `samesite=lax`, `secure` when the
  request arrived over HTTPS, with an expiry inside the signed payload.
- **Every** dashboard route goes through `require_permission`; the sidebar
  hiding a nav item is explicitly documented as a courtesy, not the control.
- Public media is HMAC-gated **and** roots-restricted — `data/inbound`
  (customers' own photos and voice notes) is unreachable even with a correctly
  computed token. Verified.
- 404, never 403, on every public-media failure: a bad token, an unknown file,
  a traversal attempt and a disallowed root are indistinguishable from outside.
- The harness ships off and is absent from the running app. Verified live.
- No raw SQL string interpolation anywhere; all queries go through SQLAlchemy.
- `httpx`/`httpcore` pinned to WARNING and the LLM key sent as a **header**,
  not a query parameter — two independent locks on the same door.
- `.env` never committed; `.gitignore` correct; git history clean.
- No CVEs in any pinned runtime dependency.
- The Instagram public surface never displays model-authored text.

---

## 9. Performance and reliability findings

### Verified sound

- **The webhook never does the work.** Claim, record, return 200; the turn
  runs on a worker thread. The endpoint is `async` and the turn is not, so
  doing it inline would block every other conversation.
- **Debouncing** turns typed fragments into one turn — a direct cost saving
  and a better reply.
- **Every HTTP call has an explicit timeout**, tiered by purpose: 8s for a
  customer-facing store read, 20s for channel sends, 60s+ for media, 120s for
  uploads. Nothing can hang a worker indefinitely.
- **Retries are targeted:** transient only (throttle, 5xx, network). Auth
  errors are deliberately not retried — they fail identically 30 seconds later
  and the customer waits for nothing.
- **One store snapshot per inbound message**, shared by every tool in the turn
  and scoped so it cannot outlive the message.
- **Throttle-aware store client** that slows down deliberately below a floor.
- **Graceful shutdown flushes the debounce buffer** on both channels, so a
  deploy does not abandon mid-sentence customers.
- **Conversation locks are refcounted**, not append-only — a previously fixed
  unbounded leak keyed on customer count.
- `pool_pre_ping` on; SQLite parity pragmas so a constraint bug cannot wait
  for production to appear.

### Findings

| # | Finding | Status |
|---|---|---|
| P1 | `webhook_events` unbounded growth | **FIXED** |
| P2 | No healthcheck — a broken boot took live traffic | **FIXED** |
| P3 | Single instance by design (in-process debouncer + scheduler) | **ACCEPTED** — documented, and both jobs are idempotent against a duplicate pass |
| P4 | Live per-message store reads are the real ceiling | **ACCEPTED** — the throttle floor is the documented signal to move to a webhook-driven cache |

---

## 10. Dead code, duplication, technical debt

**No dead code found.** Extended lint reports zero unused imports and zero
unused variables across the entire project source. No `TODO`/`FIXME`/`HACK`
markers anywhere (the only matches were `XXXX` inside phone-format comments).
No commented-out code blocks.

**Duplication is deliberate and correct where it exists.** The two Meta
signature checks share one implementation (`common/security.py`) while
Shopify's base64 variant is separate, because they genuinely differ. The
migration-tool store client and the request-path store client are deliberately
distinct: one may `sys.exit` because a person is watching, the other must never
raise past its own boundary.

### Remaining technical debt

| # | Debt | Impact |
|---|---|---|
| D1 | The commerce platform is not behind an interface — `integrations/shopify/` is ~6000 concrete lines | A second platform means extracting the abstraction first. Correct for one platform; a real cost for the second. |
| D2 | Customer-facing strings scattered across six modules | A brand rewrite means finding all of them. `REUSING_FOR_NEW_BRANDS.md` §3.3 is that list — a workaround for not having a string catalogue. |
| D3 | No formal migration tool | Mitigated by additive schema-drift reconciliation, which is well-tested. A `NOT NULL` column with no default is reported, never guessed. Acceptable at this size. |
| D4 | Region vocabulary, currency and phone normalisation are locale-shaped | Fine for one market; a named constraint for reuse. |
| D5 | 8 alias env vars documented only in `settings.py` comments | Now documented in `CONFIGURATION.md`. |
| D6 | `dashboard/shopify_api.py` (1145 lines) and `integrations/shopify/admin_products.py` (1645) are large | Both are cohesive and heavily commented; splitting for size alone is not worth the churn. |

---

## 11. Remaining risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The abandoned deployment is compromised and its store token abused | Low | **High** | ACTION-1 — delete and rotate |
| A shipping notification silently fails outside the 24h window | **High** (routine) | Medium | WARNING-1 — submit templates. Detected and alerted today; not delivered. |
| Single instance is a single point of failure | Medium | High | `restartPolicy: ON_FAILURE` + the new healthcheck. No HA today. |
| The 60-day channel token refresh breaks unnoticed | Low | High | Auto-refresh + `/health` exposes the expiry weeks early |
| Store outage refuses orders | Low | Medium | Deliberate — refusing beats overselling |
| An LLM provider serves a stack that ignores `temperature` | Medium | Medium | `require_parameters` filter is on |
| Dashboard credential brute-forced | Low | High | PBKDF2 240k + oracle closed; **no rate limiting** (S3) |

---

## 12. Requires manual verification

Nothing in the test suite can prove these. None is known to be broken; none
has been observed working during this audit.

1. **A real message from a real customer account on each channel**, end to end
   through a handset.
2. **A real order, then fulfilled in Shopify Admin**, confirming the status
   push actually reaches the phone.
3. **A proactive message outside the 24-hour window** — needs an approved
   template and cannot be simulated (blocked on WARNING-1).
4. **A real voice note and a real photo from a phone** — codecs and sizes
   differ from fixtures.
5. **An owner alert arriving in the inbox** (Resend is configured and reported
   healthy; no send was triggered).
6. **The dashboard's rendering, RTL/LTR layout and phone behaviour.**
7. **PR #3 deployed and healthy.** CI is green on all three jobs including
   PostgreSQL; the merge itself is deliberately left to a person, because
   FIXED-5 changes deployment behaviour — a failing healthcheck will now
   *block* a deploy rather than let it through. That is the intent, and it is
   worth a human pressing the button.

---

## 13. Documentation

Created in `docs/`, all brand-agnostic:

`CHATBOT.md`, `INTEGRATIONS.md`, `DATA_FLOW.md`, `DATABASE.md`,
`CONFIGURATION.md`, `DEPLOYMENT.md`, `TESTING.md`, `TROUBLESHOOTING.md`,
`REUSING_FOR_NEW_BRANDS.md`.

Updated: `ARCHITECTURE.md` (document index, full module/responsibility table,
the layering rule, corrected tool count 18 → 19), `OPERATIONS.md` (the new log
stream split and three new log lines).

Every statement is drawn from the code or from the live production
environment. Nothing was inferred from the existing documentation without
checking it against the source.

---

## 14. Final assessment

**PRODUCTION READY — with one action item outside this repository.**

The deployed system is in unusually good condition. Production is running
exactly the expected commit, with a clean boot, no runtime errors, no failed
deployments, no restart loops, and real customer traffic flowing. Every
security control this audit could test from the outside behaves correctly
against the live deployment, including the three webhook signature gates.

The codebase is exceptionally well maintained: no dead code, no unused
imports, no CVEs, no committed secrets, comprehensive tests, and — unusually —
prose comments that explain *why* each guard exists, generally by naming the
failure that produced it. That made this audit faster and its conclusions
firmer than the line count would suggest.

The five issues fixed here are all of the same kind: correct-today systems
with an unbounded or untested edge. A table that only ever grows; a log filter
that returns everything; a login that leaks a username by timing; an
interpreter that ships without being tested; a deploy that goes live without
being checked. None was breaking a customer conversation, and all five are the
kind that announce themselves only once they are expensive.

**The one thing that should not wait** is ACTION-1: an abandoned deployment
holding write credentials for the live store, with a publicly readable API
map, that nobody is watching. It is not part of this repository and cannot be
fixed by a merge — it needs the owner to delete a service and rotate two keys.

| Area | Status |
|---|---|
| Architecture | **PASS** |
| AI agent and prompts | **PASS** |
| Tools and tool calling | **PASS** |
| Shopify integration | **PASS** |
| WhatsApp integration | **PASS** |
| Instagram integration | **PASS** |
| Database and consistency | **PASS** |
| Sessions and history | **PASS** |
| Cart and order flows | **PASS** |
| Inventory and stock | **PASS** |
| Error handling and retries | **PASS** |
| Authentication and security | **FIXED** (S2) · **ACTION REQUIRED** (S1) |
| Environment and configuration | **PASS** |
| Logging and monitoring | **FIXED** |
| API integrations | **PASS** |
| Performance and reliability | **FIXED** (×2) |
| Background jobs | **PASS** |
| Deployment configuration | **FIXED** |
| Dependencies | **PASS** |
| Dead code and duplication | **PASS** |
| Technical debt | **ACCEPTABLE** — documented |
| Reusable for another brand | **YES** — see `docs/REUSING_FOR_NEW_BRANDS.md` |
