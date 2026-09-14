"""Read turn timing lines and say where the time went.

    python scripts/latency_report.py turns.log
    railway logs --service wanas --json | python scripts/latency_report.py -

Takes anything that contains the lines `common/telemetry.py` emits -- a log
file, a `railway logs` dump (plain or `--json`), a `bench_turn.py` run -- and
reports count / mean / p50 / p90 / p95 / max for the total and for every stage,
plus what share of the total each stage is.

Three things this is deliberately strict about:

* **Percentiles, not averages.** A shop's customers feel the tail. A mean of
  nine seconds built from "usually four, occasionally forty" is a number that
  describes nobody's experience, and it was the mean that made the original
  problem look smaller than it was.
* **Share of total is computed against the summed total, not the summed
  stages.** Stages overlap (a tool call contains the Shopify read inside it)
  and some time belongs to no stage at all. Normalising to 100% would hide
  exactly the gap worth finding -- so the shares do not add to 100, and the
  `unattributed` row at the bottom is what is left over.
* **A stage missing from a turn is missing, not zero.** Half the turns call no
  tool; averaging a zero into `tools` for those would make the tool loop look
  half as expensive as it is. Each stage's statistics are over the turns that
  actually had it, and `turns` says how many that was.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.telemetry import MARKER  # noqa: E402


def parse_lines(stream) -> list[dict]:
    """Every turn record in a stream of log lines.

    Handles the three shapes the same file can arrive in: our own marker in a
    plain log line, the same line wrapped in Railway's `{"message": ...}` JSON,
    and a bare record on its own line (what `bench_turn.py` writes).
    """
    turns: list[dict] = []
    for raw in stream:
        raw = raw.strip()
        if not raw:
            continue
        text = raw
        if raw.startswith("{") and MARKER not in raw:
            try:
                turns.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
            continue
        if raw.startswith("{"):
            try:
                text = str(json.loads(raw).get("message") or raw)
            except json.JSONDecodeError:
                text = raw
        index = text.find(MARKER)
        if index < 0:
            continue
        try:
            turns.append(json.loads(text[index + len(MARKER) :]))
        except json.JSONDecodeError:
            continue
    return [turn for turn in turns if isinstance(turn, dict) and "total_ms" in turn]


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. No interpolation: with twenty samples an
    interpolated p95 is a number that was never measured, and these samples are
    few enough that saying so matters."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(fraction * len(ordered) + 0.5)))
    return ordered[rank - 1]


def summarise(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean": sum(values) / len(values) if values else 0.0,
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else 0.0,
    }


def report(turns: list[dict]) -> dict:
    totals = [float(t.get("total_ms") or 0.0) for t in turns]
    stages: dict[str, list[float]] = defaultdict(list)
    for turn in turns:
        for name, ms in (turn.get("stages") or {}).items():
            stages[name].append(float(ms))

    hops = [int(t["hops"]) for t in turns if t.get("hops") is not None]
    tool_counts = [len(t.get("tools") or []) for t in turns]
    per_tool: dict[str, list[float]] = defaultdict(list)
    for turn in turns:
        for call in turn.get("tools") or []:
            per_tool[str(call.get("name"))].append(float(call.get("ms") or 0.0))
    per_hop: dict[str, list[float]] = defaultdict(list)
    for turn in turns:
        for hop in turn.get("llm") or []:
            per_hop[str(hop.get("upstream") or "unknown")].append(float(hop.get("ms") or 0.0))
    per_shopify: dict[str, list[float]] = defaultdict(list)
    for turn in turns:
        for call in turn.get("shopify") or []:
            per_shopify[str(call.get("op"))].append(float(call.get("ms") or 0.0))

    tokens = {
        "prompt": [
            float(hop["prompt_tokens"])
            for turn in turns
            for hop in turn.get("llm") or []
            if hop.get("prompt_tokens") is not None
        ],
        "completion": [
            float(hop["completion_tokens"])
            for turn in turns
            for hop in turn.get("llm") or []
            if hop.get("completion_tokens") is not None
        ],
        "reasoning": [
            float(hop["reasoning_tokens"])
            for turn in turns
            for hop in turn.get("llm") or []
            if hop.get("reasoning_tokens") is not None
        ],
        "cached": [
            float(hop["cached_tokens"])
            for turn in turns
            for hop in turn.get("llm") or []
            if hop.get("cached_tokens") is not None
        ],
    }

    grand_total = sum(totals)
    return {
        "turns": len(turns),
        "total": summarise(totals),
        "stages": {
            name: dict(
                summarise(values),
                # Against the summed total of every turn, so a stage present in
                # a third of turns reads as the third of the bill it actually
                # is -- not as whatever it costs when it happens.
                share=(sum(values) / grand_total * 100.0) if grand_total else 0.0,
            )
            for name, values in sorted(stages.items(), key=lambda kv: -sum(kv[1]))
        },
        "unattributed_share": (
            (grand_total - sum(sum(v) for k, v in stages.items() if k not in _NESTED))
            / grand_total
            * 100.0
            if grand_total
            else 0.0
        ),
        "hops": summarise([float(h) for h in hops]) if hops else None,
        "tool_calls_per_turn": summarise([float(c) for c in tool_counts]),
        "per_tool": {
            name: summarise(v) for name, v in sorted(per_tool.items(), key=lambda kv: -sum(kv[1]))
        },
        "per_upstream": {
            name: summarise(v) for name, v in sorted(per_hop.items(), key=lambda kv: -sum(kv[1]))
        },
        "per_shopify_op": {
            name: summarise(v) for name, v in sorted(per_shopify.items(), key=lambda kv: -sum(kv[1]))
        },
        "tokens": {name: summarise(v) for name, v in tokens.items() if v},
        "errors": _errors(turns),
    }


#: Stages whose time is already inside another stage, so counting them again
#: when working out what is unattributed would over-subtract. `shopify` runs
#: inside `tools`; `llm` is the model hop and stands on its own.
_NESTED = {"shopify"}


def _errors(turns: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for turn in turns:
        for key in ("error", "turn_error"):
            value = turn.get(key)
            if value:
                counts[str(value)] += 1
    return dict(counts)


def _row(name: str, stats: dict, share: float | None = None) -> str:
    share_text = f"{share:6.1f}%" if share is not None else " " * 7
    return (
        f"  {name:<22} n={stats['n']:>5}  mean={stats['mean']:>9.0f}  p50={stats['p50']:>9.0f}  "
        f"p90={stats['p90']:>9.0f}  p95={stats['p95']:>9.0f}  max={stats['max']:>9.0f}  {share_text}"
    )


def render(data: dict) -> str:
    out = [f"turns: {data['turns']}", "", "TOTAL (ms)", _row("total", data["total"])]
    out += ["", "STAGES (ms, share of all wall-clock)"]
    for name, stats in data["stages"].items():
        out.append(_row(name, stats, stats["share"]))
    blank = {"n": 0, "mean": 0, "p50": 0, "p90": 0, "p95": 0, "max": 0}
    out.append(_row("unattributed", blank, data["unattributed_share"]))

    if data.get("hops"):
        out += ["", "MODEL HOPS PER TURN", _row("hops", data["hops"])]
    out += ["", "TOOL CALLS PER TURN", _row("tool_calls", data["tool_calls_per_turn"])]

    if data["per_tool"]:
        out += ["", "PER TOOL (ms)"]
        out += [_row(name, stats) for name, stats in data["per_tool"].items()]
    if data["per_upstream"]:
        out += ["", "PER OPENROUTER UPSTREAM (ms per hop)"]
        out += [_row(name, stats) for name, stats in data["per_upstream"].items()]
    if data["per_shopify_op"]:
        out += ["", "PER SHOPIFY OPERATION (ms)"]
        out += [_row(name, stats) for name, stats in data["per_shopify_op"].items()]
    if data["tokens"]:
        out += ["", "TOKENS PER HOP"]
        out += [_row(name, stats) for name, stats in data["tokens"].items()]
    if data["errors"]:
        out += ["", "ERRORS"] + [f"  {name}: {count}" for name, count in data["errors"].items()]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("source", nargs="?", default="-", help="log file, or - for stdin")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--channel", default="", help="only turns on this channel (whatsapp, instagram, bench)"
    )
    args = parser.parse_args(argv)

    if args.source == "-":
        turns = parse_lines(sys.stdin)
    else:
        with open(args.source, encoding="utf-8", errors="replace") as handle:
            turns = parse_lines(handle)

    if args.channel:
        turns = [t for t in turns if t.get("ch") == args.channel]

    if not turns:
        print("no turn timing lines found", file=sys.stderr)
        return 1

    data = report(turns)
    print(json.dumps(data, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
