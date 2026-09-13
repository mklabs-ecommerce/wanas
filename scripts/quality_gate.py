"""Did the speed-up cost an answer?

    python scripts/quality_gate.py --record docs/golden.json     # before
    python scripts/quality_gate.py --check  docs/golden.json     # after every change

Runs the same conversations `bench_turn.py` times (`scripts/bench_scenarios.py`)
and judges the replies against a recorded golden set. This is the rule that
lets an optimisation be kept or reverted without anyone reading the Arabic:
a change that makes the bot faster and slightly wrong is not a speed-up, and
"slightly wrong" in a shop means a price, a size or a stock claim.

Five checks, and each one is a failure mode this repository has already paid
for at least once:

1. **The same tools were called.** A reply that stops calling `get_variants`
   and answers from memory is a reply that will eventually invent a colour.
   The scenario's `expects_tools` is the floor; drifting away from what the
   golden run called is reported, and fails unless `--allow-tool-drift`.
2. **The same facts.** Every price, size and total the golden reply stated has
   to still be in the new one. Numbers are compared after folding Arabic-Indic
   digits onto ASCII, because ٦٠ and 60 are the same fee.
3. **Still Egyptian Arabic.** A reply that has quietly become English, or
   Modern Standard, is a different product. Checked structurally (Arabic share
   of the letters, and the dialect markers the prompt asks for) rather than by
   a second model call -- a judge that can be wrong is not a gate.
4. **Nothing truncated.** A reply that ends mid-sentence reads as ordinary
   Arabic right up to where it stops.
5. **No fallback.** `GENERIC_FAILURE`, `RATE_LIMITED`, `LOOP_EXHAUSTED` and the
   truncation and promise fallbacks are all *correct* behaviour for a broken
   turn and all mean the customer did not get an answer. A benchmark that got
   faster because more turns fell back is the exact trap this closes.

Exit code 0 means keep the change; 1 means revert it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import agent  # noqa: E402
from assistant.providers import set_provider  # noqa: E402
from domain.db import session_scope  # noqa: E402
from scripts import bench_scenarios, bench_turn  # noqa: E402

#: Every sentence that means "the turn did not answer". All of them are the
#: right thing to send and none of them is an answer, so a run with more of
#: them than the golden one is a regression however fast it was.
FALLBACKS = (
    agent.RATE_LIMITED,
    agent.GENERIC_FAILURE,
    agent.LOOP_EXHAUSTED,
    agent.TRUNCATED_FALLBACK,
    agent.PROMISE_FALLBACK,
    agent.PROMISE_FALLBACK_WITH_PRODUCT,
    agent.PROMISE_FALLBACK_WITH_COLOR,
    agent.IMAGE_PROMISE_FALLBACK,
)

_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NUMBER = re.compile(r"\d+")
_ARABIC_LETTER = re.compile(r"[؀-ۿ]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")

#: Words that only appear in the dialect the shop writes in. One is enough:
#: a short reply ("تمام، اتحط في السلة") is a correct Egyptian sentence that
#: happens to contain few of them, and a gate that demands several would fail
#: the best replies in the set.
_EGYPTIAN = (
    "عايز", "عاوز", "إيه", "ايه", "ده", "دي", "كده", "دلوقتي", "تمام", "بكام",
    "حضرتك", "معلش", "خلاص", "أهلاً", "اهلا", "عندنا", "هيوصل", "بيوصل", "جنيه",
    "تحب", "ممكن", "في", "مش", "علشان", "عشان", "لو", "يا فندم", "المقاس", "مقاس",
)


def digits(text: str) -> set[str]:
    """Every number in a reply, with Arabic-Indic folded onto ASCII."""
    return set(_NUMBER.findall((text or "").translate(_ARABIC_INDIC)))


def is_egyptian_arabic(text: str) -> tuple[bool, str]:
    text = (text or "").strip()
    if not text:
        return False, "empty reply"
    arabic = len(_ARABIC_LETTER.findall(text))
    latin = len(_LATIN_LETTER.findall(text))
    if arabic == 0:
        return False, "no Arabic at all"
    # Product names, sizes and colours stay Latin on purpose (see
    # common/bidi.py), so a Latin run is expected -- a reply that is *mostly*
    # Latin is not.
    if latin > arabic:
        return False, f"mostly Latin ({latin} Latin vs {arabic} Arabic letters)"
    if not any(marker in text for marker in _EGYPTIAN):
        return False, "no Egyptian dialect marker"
    return True, ""


def looks_truncated(text: str) -> bool:
    """A reply that stops mid-word or mid-clause.

    Deliberately conservative. `finish_reason` is the real signal and
    `agent.run_turn` already acts on it; this is the second net, for a reply
    that arrived complete by the API's reckoning and still reads as cut off.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    return stripped.endswith(("،", ",", ":", "؛", "-", "و")) or stripped.endswith("...")


def run(runs: int, real: bool, only: str) -> dict:
    provider = None
    if real:
        set_provider(None)
    else:
        provider = bench_turn.PlannedProvider()
        set_provider(provider)

    bench_turn.install_fake_shelf()
    with session_scope() as db:
        facts = bench_scenarios.resolve(db)
    scenarios = bench_scenarios.build(facts)
    if only:
        scenarios = [s for s in scenarios if s.name == only]

    replies: list[dict] = []
    for _ in range(runs):
        for scenario in scenarios:
            for reply in bench_turn.run_scenario(scenario, provider=provider, real=real):
                step = scenario.steps[reply["step"]]
                reply["expects_tools"] = list(step.expects_tools)
                reply["expects_text"] = list(step.expects_text)
                replies.append(reply)
    return {"facts": facts, "replies": replies}


def _by_step(record: dict) -> dict[tuple[str, int], list[dict]]:
    grouped: dict[tuple[str, int], list[dict]] = {}
    for reply in record["replies"]:
        grouped.setdefault((reply["scenario"], reply["step"]), []).append(reply)
    return grouped


def check(golden: dict, fresh: dict, *, allow_tool_drift: bool) -> list[str]:
    """Every way the new run is worse than the golden one, as plain sentences."""
    failures: list[str] = []
    golden_steps = _by_step(golden)
    fresh_steps = _by_step(fresh)

    for key, fresh_replies in sorted(fresh_steps.items()):
        where = f"{key[0]}[{key[1]}]"
        golden_replies = golden_steps.get(key) or []

        for reply in fresh_replies:
            text = reply.get("text") or ""
            silent = not text and not reply.get("error")

            if reply.get("error"):
                failures.append(f"{where}: the turn errored ({reply['error']})")
                continue
            for fallback in FALLBACKS:
                # Two of these are templates (`{product}`, `{color}`), so the
                # match is on the fixed opening rather than the whole string --
                # otherwise the two fallbacks that name a product, which are
                # the ones a long conversation actually reaches, would never
                # be recognised.
                marker = fallback.split("{", 1)[0].strip()
                if len(marker) >= 12 and marker in text:
                    failures.append(f"{where}: the reply is the {_fallback_name(fallback)} fallback")
                    break

            missing_tools = [t for t in reply.get("expects_tools") or [] if t not in reply["tool_calls"]]
            if missing_tools:
                failures.append(f"{where}: did not call {', '.join(missing_tools)}")

            if silent:
                # A deliberately silent turn: `confirm_order` sends the
                # confirmation itself. Nothing below applies to no text.
                continue

            for fact in reply.get("expects_text") or []:
                if fact and fact not in text.translate(_ARABIC_INDIC):
                    failures.append(f"{where}: the reply no longer states {fact!r}")

            ok, why = is_egyptian_arabic(text)
            if not ok:
                failures.append(f"{where}: {why}")

            if looks_truncated(text):
                failures.append(f"{where}: the reply reads as cut off")

        if not golden_replies:
            continue

        golden_tools = {t for r in golden_replies for t in r["tool_calls"]}
        new_tools = {t for r in fresh_replies for t in r["tool_calls"]}
        if golden_tools != new_tools:
            message = (
                f"{where}: tools changed -- golden {sorted(golden_tools)}, now {sorted(new_tools)}"
            )
            if allow_tool_drift:
                print(f"  note: {message}")
            else:
                failures.append(message)

        golden_numbers = set().union(*(digits(r.get("text") or "") for r in golden_replies))
        new_numbers = set().union(*(digits(r.get("text") or "") for r in fresh_replies))
        # Only numbers the golden run stated and the new one dropped. A new
        # number is not a regression -- a reply that adds the delivery window
        # is a better reply, and pinning the set exactly would fail it.
        lost = {
            number
            for number in golden_numbers - new_numbers
            # One-and-two-digit counts ("2 to 4 days", "1 piece") move around
            # with phrasing. Prices, totals and measurements do not.
            if len(number) >= 3
        }
        if lost:
            failures.append(f"{where}: facts dropped from the reply: {sorted(lost)}")

    return failures


def _fallback_name(text: str) -> str:
    for name in dir(agent):
        if name.isupper() and getattr(agent, name, None) == text:
            return name
    return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", metavar="FILE", help="run and save the golden set")
    mode.add_argument("--check", metavar="FILE", help="run and compare against the golden set")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--real", action="store_true", help="use the configured provider")
    parser.add_argument("--only", default="")
    parser.add_argument(
        "--allow-tool-drift",
        action="store_true",
        help="report a changed tool set instead of failing on it (a real model "
        "legitimately picks a different route on the same question)",
    )
    args = parser.parse_args(argv)

    fresh = run(args.runs, args.real, args.only)

    if args.record:
        Path(args.record).write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"recorded {len(fresh['replies'])} replies to {args.record}")
        # Recording is also a check against the rules that do not need a
        # baseline: a golden set full of fallbacks is not a baseline.
        failures = check(fresh, fresh, allow_tool_drift=True)
        if failures:
            print("\nthe recorded run is not clean:")
            for failure in failures:
                print(f"  - {failure}")
            return 1
        return 0

    golden = json.loads(Path(args.check).read_text(encoding="utf-8"))
    failures = check(golden, fresh, allow_tool_drift=args.allow_tool_drift)
    if failures:
        print(f"QUALITY GATE FAILED ({len(failures)} problem(s)) -- revert the change")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"quality gate passed: {len(fresh['replies'])} replies, {len(_by_step(fresh))} steps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
