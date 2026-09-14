"""Run the fixed scenarios N times and say how long each part took.

    python scripts/bench_turn.py --runs 5                    # fake provider
    LLM_PROVIDER=openrouter python scripts/bench_turn.py --runs 3 --real

Two modes, and the gap between them is the whole point:

* **fake** (`scripts/bench_scenarios.py` drives a planned provider) measures
  *this codebase*: history load, context build, tool execution, the Shopify
  reads, the session writes, the number of round trips. The model's own
  latency is removed, which is the only way to tell "our code is slow" from
  "the model is slow" without guessing.
* **`--real`** sends the same customer messages to the configured provider and
  measures the whole picture.

What it does **not** measure is the debounce window or the Meta send: neither
is reachable without a webhook and a credential, both are fixed costs the
report adds back, and pretending to measure them here would only produce a
number nobody could check. `docs/PERFORMANCE.md` states them separately.

Each turn emits the same `common/telemetry.py` line production emits, so
`scripts/latency_report.py` reads a benchmark run and a day of real traffic
with the same code and the same definitions.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import runtime  # noqa: E402
from assistant.providers import set_provider  # noqa: E402
from assistant.providers.base import LLMProvider, ModelReply  # noqa: E402
from common import telemetry  # noqa: E402
from domain.db import session_scope  # noqa: E402
from domain.models import ShippingRate  # noqa: E402
from integrations.shopify import catalog as shopify_catalog  # noqa: E402
from scripts import bench_scenarios, latency_report  # noqa: E402


class PlannedProvider(LLMProvider):
    """A model with the thinking taken out and the shape left in.

    It replays one scenario's planned hops in order: the same tool calls, the
    same number of round trips, the same final sentence. Nothing about the
    tool loop, the history, the context build or the Shopify read is faked --
    only the part that costs seconds on a wire.

    Deliberately *not* `RehearsalProvider`: that one maps English commands to
    a single tool call, so every turn it drives is one hop, and a one-hop
    benchmark cannot see the cost of the multi-hop turns that are most of
    production.
    """

    name = "planned"
    supports_audio = True
    supports_vision = True

    def __init__(self) -> None:
        self.queue: list = []
        self.hops = 0

    def load(self, plan: list) -> None:
        self.queue = list(plan)

    def generate(self, system_prompt: str, history: list[dict], tools: list) -> ModelReply:
        self.hops += 1
        if not self.queue:
            return ModelReply(text="تمام.")
        head = self.queue.pop(0)
        if isinstance(head, str):
            return ModelReply(text=head)
        return ModelReply(
            tool_calls=[
                {"id": f"call_{index}", "name": name, "arguments": dict(arguments)}
                for index, (name, arguments) in enumerate(head)
            ]
        )

    def transcribe(self, audio: bytes, mime_type: str, *, hint: str = "") -> str:
        return ""

    def inspect_image(self, image: bytes, mime_type: str, *, catalog: list[dict]):
        raise NotImplementedError


def run_scenario(
    scenario: bench_scenarios.Scenario, *, provider: PlannedProvider | None, real: bool
) -> list[dict]:
    """One conversation, start to finish, on a customer nobody else is using.

    A fresh external_id per run on purpose: a benchmark whose second run reads
    a history the first run wrote is measuring a longer conversation, not the
    same one twice, and the context build is one of the stages being watched.
    """
    external_id = f"bench-{uuid.uuid4().hex[:12]}"
    replies: list[dict] = []

    for index, step in enumerate(scenario.steps):
        if provider is not None:
            provider.load(step.plan)
        started = time.perf_counter()
        with telemetry.turn(
            "bench", external_id, scenario=scenario.name, step=index, real=real
        ), shopify_catalog.turn_scope(), session_scope() as db:
            reply = runtime.handle_message(
                "whatsapp",
                external_id,
                step.text,
                db=db,
                platform_message_id=f"{external_id}-{index}",
            )
        replies.append(
            {
                "scenario": scenario.name,
                "step": index,
                "text": reply.text or "",
                "tool_calls": list(reply.tool_calls),
                # How many pictures actually went out. Only `get_variants`
                # attaches one, so this is the number that says whether a
                # change which let the model answer *without* calling it has
                # quietly stopped the customer seeing the product.
                "attachments": len(reply.attachments or []),
                # And how many of them were the size chart rather than the
                # garment. The two are opposite failures -- too few product
                # photos, and a measurements table on a reply about price --
                # so a single count cannot tell the gate about either.
                # Labelled by `tools.base._chart_label`, which is the only
                # place a chart attachment is ever named.
                "charts": sum(
                    1
                    for label in (reply.attachment_labels or {}).values()
                    if str(label).endswith("size chart")
                ),
                "error": reply.error,
                "seconds": time.perf_counter() - started,
            }
        )
    return replies


def install_fake_shelf() -> object | None:
    """An in-memory Shopify shelf, so the order path is measured rather than
    refused.

    `confirm_order` asks Shopify whether the sale may happen and correctly
    refuses when it cannot reach it, so without a shelf the most expensive
    scenario in the set never runs at all. The suite's own fake
    (`tests/fake_shopify.py`) is a working shelf rather than a mock, which is
    exactly what a benchmark wants.

    **And it is the only shelf this script will ever use.** Pointing a
    benchmark at the real store would place real cash-on-delivery orders and
    decrement real stock every run -- see `docs/PERFORMANCE.md` for why the
    real-model mode still keeps the shop out of it.
    """
    try:
        import pytest

        from tests.fake_shopify import FakeShopify
    except ImportError:
        print("tests/ or pytest unavailable: the order scenario will be refused", file=sys.stderr)
        return None

    patch = pytest.MonkeyPatch()
    fake = FakeShopify()
    fake.install(patch)
    with session_scope() as db:
        fake.seed_from(db)
        rate = db.get(ShippingRate, "Cairo")
        if rate is not None and not rate.fee:
            # Fees ship blank; an unpriced governorate refuses the order and
            # the scenario measures a refusal instead of a sale.
            rate.fee = 60
    return patch


class _Collector(logging.Handler):
    """Keeps the turn lines this run produced, so the report is of this run
    and not of whatever else is in the log file."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.turns: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(telemetry.MARKER):
            try:
                self.turns.append(json.loads(message[len(telemetry.MARKER) :]))
            except json.JSONDecodeError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runs", type=int, default=5, help="times through the whole set")
    parser.add_argument(
        "--real",
        action="store_true",
        help="use the configured provider instead of the planned one",
    )
    parser.add_argument("--only", default="", help="one scenario by name")
    parser.add_argument("--out", default="", help="write the raw turn lines here")
    parser.add_argument("--replies", default="", help="write the replies here (for quality_gate)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument(
        "--live-shopify",
        action="store_true",
        help="read the configured Shopify store instead of the in-memory shelf "
        "(reads only -- the order scenario is skipped, see docs/PERFORMANCE.md)",
    )
    args = parser.parse_args(argv)

    os.environ.setdefault("LATENCY_LOG", "1")

    collector = _Collector()
    logging.getLogger("wanas.latency").addHandler(collector)
    logging.getLogger("wanas.latency").setLevel(logging.INFO)

    provider: PlannedProvider | None = None
    if args.real:
        set_provider(None)
    else:
        provider = PlannedProvider()
        set_provider(provider)

    if args.live_shopify:
        # Reads are fine and are what a real turn does; writing is not. The
        # order scenario is the only one that writes, and it is dropped rather
        # than left to place a real cash-on-delivery order on every run.
        scenario_filter = {"confirm_order"}
    else:
        install_fake_shelf()
        scenario_filter = set()

    with session_scope() as db:
        facts = bench_scenarios.resolve(db)
    scenarios = [s for s in bench_scenarios.build(facts) if s.name not in scenario_filter]
    if args.only:
        scenarios = [s for s in scenarios if s.name == args.only]
        if not scenarios:
            print(f"no scenario named {args.only!r}", file=sys.stderr)
            return 1

    replies: list[dict] = []
    for run in range(args.runs):
        for scenario in scenarios:
            try:
                replies.extend(run_scenario(scenario, provider=provider, real=args.real))
            except Exception as exc:  # a broken scenario must not lose the rest
                print(f"run {run} scenario {scenario.name} failed: {exc}", file=sys.stderr)

    if args.out:
        Path(args.out).write_text(
            "\n".join(json.dumps(t, ensure_ascii=False) for t in collector.turns),
            encoding="utf-8",
        )
    if args.replies:
        Path(args.replies).write_text(
            json.dumps({"facts": facts, "replies": replies}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if not collector.turns:
        print("no turns were measured", file=sys.stderr)
        return 1

    data = latency_report.report(collector.turns)
    data["mode"] = "real" if args.real else "fake"
    data["runs"] = args.runs
    data["per_scenario"] = {
        name: latency_report.summarise(
            [float(t["total_ms"]) for t in collector.turns if t.get("scenario") == name]
        )
        for name in sorted({str(t.get("scenario")) for t in collector.turns})
    }

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"mode: {data['mode']}   runs: {args.runs}")
        print(latency_report.render(data))
        print("\nPER SCENARIO (ms, whole turn)")
        for name, stats in data["per_scenario"].items():
            print(latency_report._row(name, stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
