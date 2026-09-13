"""What real customers actually waited, read out of the transcript itself.

    railway run --service wanas -- python scripts/prod_turn_latency.py --days 30

The timing line (`common/telemetry.py`) only exists from the deploy that added
it onwards, and the question that had to be answered first was "how slow is it
*now*". The answer was already in the database: every stored message carries
`at` (`assistant/messages.py`), so for each customer message followed by a
reply, the gap between the two is that turn's duration.

Two things this measures and two it does not, stated plainly because the number
is quoted in `docs/PERFORMANCE.md`:

* It **does** measure the agent turn end to end -- history load, every model
  hop, every tool call, the Shopify reads, the session write -- for real
  customers, on real conversations, at whatever length they had grown to.
* It does **not** include the debounce window or the webhook's own ingest
  work. The stored user message is stamped when the *turn* starts, which is
  after the window closed. `MESSAGE_DEBOUNCE_SECONDS` and the measured ingest
  cost are added back in the document rather than guessed at here.
* It does **not** include the Meta send, which happens after the reply is
  stored.
* A `by="system"` message is not a turn. Confirmations, status pushes and
  cart nudges are written by `domain/services/notifications.py` at whatever
  time the shop decided them, and counting one as an answer to the customer's
  last message produces gaps measured in hours.

Strictly read-only: one SELECT, no writes, no schema access. Safe to point at
production, which is the only place that has real customers in it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from common.timeutil import as_aware, utcnow  # noqa: E402
from config.settings import settings  # noqa: E402
from scripts.latency_report import summarise  # noqa: E402

#: A gap longer than this is not a slow turn. It is a conversation that was
#: paused for staff, a turn that crashed and was answered by the next message,
#: or a customer who came back the next morning -- all real, none of them
#: latency. Ten minutes is far above anything the model has ever taken and far
#: below the six-hour expiry.
MAX_PLAUSIBLE_SECONDS = 600.0


def _parse(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        return as_aware(datetime.fromisoformat(str(stamp)))
    except (TypeError, ValueError):
        return None


def turns(history: list, since: datetime) -> list[dict]:
    """Every (customer message -> reply) pair in one conversation."""
    out: list[dict] = []
    pending: datetime | None = None
    for message in history:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        at = _parse(message.get("at"))

        if role == "user":
            # A provisional copy is the same message written on arrival; the
            # real one replaces it, so the later stamp is the turn's start.
            pending = at
            continue
        if role != "assistant" or pending is None:
            continue
        if message.get("by") in {"system", "staff"}:
            # Not an answer to this message: a status push, a nudge, or a
            # person typing in the dashboard. The customer's message is still
            # waiting, so `pending` is deliberately left where it is.
            continue
        if not message.get("content"):
            # An assistant message with no words is a tool-call hop inside the
            # turn, not the reply.
            continue
        if at is None or at < since:
            pending = None
            continue

        seconds = (at - pending).total_seconds()
        pending = None
        if 0 <= seconds <= MAX_PLAUSIBLE_SECONDS:
            out.append({"seconds": seconds, "at": at.isoformat()})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--channel", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--url", default="", help="defaults to DATABASE_URL")
    args = parser.parse_args(argv)

    url = args.url or settings.database_url
    since = utcnow() - timedelta(days=args.days)

    engine = create_engine(url, pool_pre_ping=True)
    query = "SELECT channel, history FROM sessions"
    if args.channel:
        query += " WHERE channel = :channel"

    measured: list[dict] = []
    per_channel: dict[str, list[float]] = {}
    conversations = 0
    with engine.connect() as connection:
        for channel, history in connection.execute(
            text(query), {"channel": args.channel} if args.channel else {}
        ):
            if isinstance(history, str):
                try:
                    history = json.loads(history)
                except json.JSONDecodeError:
                    continue
            if not isinstance(history, list):
                continue
            conversations += 1
            found = turns(history, since)
            measured.extend(found)
            per_channel.setdefault(str(channel), []).extend(t["seconds"] for t in found)

    if not measured:
        print(f"no answered turns in the last {args.days:g} days", file=sys.stderr)
        return 1

    seconds = [t["seconds"] for t in measured]
    data = {
        "days": args.days,
        "conversations": conversations,
        "turns": len(seconds),
        "turn_seconds": {k: round(v, 2) for k, v in summarise(seconds).items()},
        "by_channel": {
            name: {k: round(v, 2) for k, v in summarise(values).items()}
            for name, values in sorted(per_channel.items())
        },
        "note": "agent turn only -- excludes the debounce window, the webhook "
        "ingest work and the Meta send",
    }

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"window: last {args.days:g} days")
        print(f"conversations scanned: {conversations}")
        print(f"answered turns measured: {len(seconds)}")
        stats = data["turn_seconds"]
        print(
            f"turn seconds  mean={stats['mean']}  p50={stats['p50']}  "
            f"p90={stats['p90']}  p95={stats['p95']}  max={stats['max']}"
        )
        for name, values in data["by_channel"].items():
            print(
                f"  {name:<12} n={values['n']:>5}  mean={values['mean']:>7}  "
                f"p50={values['p50']:>7}  p90={values['p90']:>7}  p95={values['p95']:>7}"
            )
        print(f"\n{data['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
