# Deployment

How the system is built, started, configured, monitored and maintained in
production.

No credentials appear here. See [CONFIGURATION.md](CONFIGURATION.md) for what
each variable does and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for what to do
when something is wrong.

---

## The shape of it

```
   GitHub  ──── push / merge to main ────►  Railway
   repo                                     builds with Railpack (Python)
     │                                      installs requirements.txt
     │                                      starts: uvicorn app:app
     │                                            │
     └── CI on every push and PR                  ├── web service (1 replica)
         ├─ ruff check .                          └── PostgreSQL service
         ├─ pytest on Python 3.11
         ├─ pytest on Python 3.13   ← the deployed version
         └─ pytest on PostgreSQL 16
```

**One deployed process.** A modular monolith: at this volume five separately
deployed services would be more operational overhead than the problem needs.
The consequences are stated where they matter — the debouncer and the
scheduler are both in-process and sized for one instance.

---

## Build

Railpack detects Python, reads the version, and installs from
`requirements.txt`. No Dockerfile.

**Dependencies are pinned, not ranged.** The platform rebuilds from that file
on every deploy, so an unranged dependency means the build that ships is not
the build that was tested, and the difference only shows up in production.
Bump deliberately, with the suite green.

**The Python version is pinned too, for the same reason.** `.python-version`
declares it and CI tests it. Without a pin, the builder resolves whatever
satisfies `requires-python` in `pyproject.toml`, so a builder release can move
production onto a new interpreter with no diff anywhere in the repository.

Current runtime: **Python 3.13**, FastAPI + uvicorn, SQLAlchemy 2, psycopg 3,
httpx. Seven runtime dependencies in total.

---

## Start command

```
uvicorn app:app --host 0.0.0.0 --port $PORT
```

`app:app` is the composition root — the only file that wires the pieces
together. **Do not change this.** It is written down in three places that must
agree: the service settings, `Procfile`, and this document.

`railway.toml` deliberately does **not** restate it: that file exists for the
healthcheck, and the platform merges only the keys it names, so restating the
start command is the one way that file could silently change it.

---

## Startup sequence

Everything in `app.py`'s lifespan. Each optional step logs and swallows its own
failures — a piece of startup configuration must never be what stops the app
booting.

```
 1  create_all                     add missing TABLES
 2  _ensure_schema_columns         add missing COLUMNS (additive, idempotent)
 3  _ensure_catalog_seeded         seed only if the catalog is empty
 4  _ensure_shipping_fees_set      fill any rate still NULL (never overwrite)
 5  register the callbacks         history clearer · transcript recorder · mailer
 6  register outbound senders      one per channel, never a shared default
 7  refresh the channel token      first check at boot
 8  loud warnings                  every silently-expensive misconfiguration
 9  background threads             catalogue reconcile · webhook registration
10  scheduler.start()
    ── serving ──
11  scheduler.stop(); dispatcher.shutdown(wait=True) on BOTH channels
```

Step 11 matters on every deploy: the dispatcher's `shutdown` **flushes** what
is still inside the debounce window rather than dropping it. Those customers
have already been recorded as having written, so a dropped buffer reads as
unanswered forever. Missing either channel's shutdown drops that channel's
buffered messages on every deploy.

Steps 1–2 exist because there is no migration tool: `create_all` adds tables
and silently ignores columns. That gap once cost four days of orders — each
one created on the commerce platform, failing its local `INSERT`, and being
cancelled again by the compensating path, with nothing in the boot log.

---

## Healthcheck

`railway.toml` points the platform's healthcheck at `/health` with a 300s
timeout.

Without it, traffic switches as soon as the process is **listening**, and
listening is not working. This app does real work at startup, and a container
that bound the port and then failed any of it would be sent live customer
messages while the healthy one it replaced was already gone. The restart
policy catches a process that *dies*; nothing caught one that came up *wrong*.

`/health` opens a database session and counts the catalog, so it answers 200
only when the process can reach the database and has something to sell. When
it does not, the deploy fails and the previous container keeps serving — which
is the right outcome for every one of those failures.

The timeout is generous on purpose: a healthcheck tight enough to fail a
slow-but-fine start is worse than none.

---

## Monitoring

### `/health`

The single endpoint that answers what is wired up. Every field is a boolean or
a count; no value is ever revealed. Poll it externally — a 200 with
`catalog_products: 0`, or `shopify_webhooks_configured: false`, is a total
silent failure of some feature that looks perfectly healthy from the outside.

Also watch `instagram_token_expires_at`: it is the 60-day token's remaining
life, and a broken refresh job is visible weeks before the channel goes quiet.

### Logs

INFO and below go to **stdout**; WARNING and above go to **stderr**.

That split is deliberate and load-bearing on this host: the platform files a
log line's severity by the stream it arrived on. With everything on stderr —
which is what `logging.basicConfig` does — every routine line was labelled
`error`, so filtering the deploy's logs for errors returned the whole log.

So: **anything in stderr is something to look at.** `httpx`/`httpcore` are
pinned to WARNING so no vendor client can leak a URL-borne credential into a
routine log line.

Lines worth alerting on:

| Line | Means |
|---|---|
| `rejected a webhook with a bad signature` | someone is probing, or a secret is wrong |
| `Refusing an order: Shopify unreachable` | sales are being turned away |
| `LEFT OPEN -- cancel it by hand` | a remote order exists with no local row |
| `the local write failed at stage=…` | the compensating path ran |
| `released N webhook claim(s) after a failed turn` | a turn crashed |
| `dropping inbound message: conversation … is paused` | somebody is waiting on a person |
| `no inbound message extracted from a … delivery` | a payload shape changed |
| `could not reconcile the database schema` | schema drift needs a person |

### Owner alerts

Email, through Resend (or the Gmail API, or SMTP off-platform). Only what
someone must act on *now*: a complaint, a crashed or undeliverable turn, an
order modified/cancelled/swapped, low stock, and **every** handoff — a handoff
pauses the conversation, so nobody is answering that customer until a person
opens the dashboard.

Order confirmations are deliberately excluded. They fire on every sale, and an
address carrying those is an address that gets filtered — which would cost all
of the above.

### The dashboard

The staff control surface, and in practice the real monitor: conversations
(with an `unanswered` filter built on exactly the provisional-message
mechanism described in [DATA_FLOW.md](DATA_FLOW.md)), the review queue,
store-wide statistics, and the comments screen.

---

## Environments

| | Local | Production |
|---|---|---|
| Database | SQLite (refused in a deploy) | PostgreSQL |
| LLM | `fake`, or a real key | the configured provider |
| Debounce | often `0` (inline) | 6s |
| Harness | optional | **off** |
| Debug | optional | **off** |
| Scheduler | usually off | 1800s |

There is no staging environment. The PR's CI — three jobs including a real
PostgreSQL — is what stands in for one, which makes the PostgreSQL job worth
more than it looks: SQLite and Postgres do not serialise a concurrent
decrement the same way, and that difference has already broken every order
placement in production once.

---

## Deploying a change

```
1  branch, change, and add or update tests
2  make check                      ruff + the full suite
3  WANAS_TEST_DATABASE_URL=postgresql+psycopg://… make test    (or let CI do it)
4  push, open a PR
5  wait for all three CI jobs
6  merge to main → the host builds and deploys automatically
7  curl https://<domain>/health
8  read the boot log for warnings
```

Rollback is a redeploy of the previous deployment from the host's dashboard.
There are no down-migrations: schema changes are additive by construction, so
the previous version runs unchanged against the newer schema.

### Changes that need more than a merge

| Change | Also required |
|---|---|
| A new `NOT NULL` column with no default | it is **reported, never guessed at** — apply it by hand |
| A new webhook topic | re-run registration, or restart (it is idempotent at boot) |
| Rotating an app secret | update the variable **and** the platform's app config, or inbound refuses everything |
| A new proactive message | submit and get the template approved first, or it cannot leave the 24-hour window |

---

## Maintenance

All scripts are **dry-run by default**, idempotent, and need `--apply`.

| Script | Does |
|---|---|
| `manage.py seed` | import the catalogue and region list |
| `manage.py create-staff` | add a dashboard account |
| `manage.py set-fee` | set a region's shipping fee |
| `scripts/migrate_schema.py` | add every column the models declare and the database lacks |
| `scripts/shopify_sync.py` | ongoing catalogue/stock reconciliation |
| `scripts/shopify_check_live.py` | read-only smoke check against the live store |
| `scripts/shopify_set_skus.py` | link local variant ids to platform SKUs |
| `scripts/shopify_reconcile_products.py` | **the only deleting one.** Removes local products the platform no longer has; archives anything ever ordered; refuses an empty or mostly-empty live read. |
| `scripts/shopify_size_charts.py` / `_import.py` | publish charts as metafields, and read them back |
| `scripts/shopify_backfill_customers.py` | attach customers to orders placed before the bot did so |
| `scripts/gmail_authorise.py` | mint a Gmail API refresh token |

The reconcile is split on purpose: the **reporting** half runs unattended at
every boot, and the **deleting** half is manual. A reconcile that deletes
unattended is one bad live read away from an empty catalog.

---

## Scaling notes

Two things are in-process and single-instance by design:

- **the debouncer** — two instances debounce independently, which is correct,
  just less effective; a restart loses what is buffered, which is why the
  idempotency claim is taken at ingest;
- **the scheduler** — two instances each run their own copy, and both jobs are
  idempotent against a duplicate pass, so that degrades to "maybe checked
  twice in the same minute", never a double message or a missed one.

Moving either to Redis or a real cron means replacing that one file, not its
callers.

The live-per-message read of the commerce platform is the real operational
limit. The client tracks the platform's throttle status and slows down
deliberately below a floor; regularly hitting that floor is the signal to move
to a webhook-driven cache.

---

## Cost and capacity

Per customer message: one to three model calls (the tool loop), one platform
read for the catalogue snapshot (shared across every tool in the turn), plus
one fresh read at order time. Media adds one call per photo or voice note.

Debouncing is a direct saving: three typed fragments cost one turn, not three.
`MESSAGE_WORKERS` (8) caps how many different conversations run at once.
