"""Did the speed-up cost an answer?

    python scripts/quality_gate.py --record docs/golden.json     # before
    python scripts/quality_gate.py --check  docs/golden.json     # after every change

Runs the same conversations `bench_turn.py` times (`scripts/bench_scenarios.py`)
and judges the replies against a recorded golden set. This is the rule that
lets an optimisation be kept or reverted without anyone reading the Arabic:
a change that makes the bot faster and slightly wrong is not a speed-up, and
"slightly wrong" in a shop means a price, a size or a stock claim.

Ten checks, and each one is a failure mode this repository has already paid
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
6. **Only the two ways this shop can actually be paid.** Cash at the door, or
   online through the website -- 43eb403 settled that pair after the prompt and
   `assistant/comment_faq.py` had been answering the question differently
   depending on whether it was asked in a DM or in a comment, and the prompt now
   requires the sentence verbatim. Anything else -- a card in the chat, InstaPay,
   a wallet, a payment link -- is a promise the shop cannot keep, made to a
   customer who may act on it. This rule exists because a candidate model
   measured for a possible swap offered exactly that on its first run through
   these scenarios, and every other check passed it.
7. **The shop's name is spelled the one way.** `Wanas Gallery`, short form
   `Wanas`. The model meets the brand in four surface forms and Arabic writes
   no short vowels, so «ونس» is literally w-n-s -- which is how a reply ends up
   saying `Wnas` or offering `WNS` as the shop's name. A customer told the shop
   is called something it is not has been given wrong information about who
   they are buying from.
8. **Laid out so it reads the way it was written.** Arabic with Latin and
   numbers inside it is the normal case here, and two shapes of line come
   out reordered on the phone: one opening with a Latin word takes
   left-to-right direction for the whole line, and a number separated from
   a Latin word by a dash or a comma swaps places with it -- «لون Black —
   590 جنيه» is displayed «لون 590 — Black جنيه». `common/bidi.py` repairs
   both at the send boundary, and this rule keeps them from being written
   in the first place: the dashboard shows staff the unrepaired string, and
   every shape the prompt asks for already renders correctly on its own.
9. **A sleeve question is answered, not deflected.** `Product.sleeve` is a
   catalog field, so "I have no data about sleeve length" beside the words
   «نص كم» is the bot refusing to read something it is holding -- which is
   exactly the reply that made the field necessary.
10. **Photographs still go out.** Only `get_variants` attaches one, so a change
   that lets the model answer a product question without calling it can quietly
   stop a clothes shop ever showing the clothes. Judged against the golden run:
   sending fewer is a judgement, sending none is a regression.

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


#: The ways of paying this shop cannot take. It takes two -- cash at the door,
#: and the website's own online checkout -- and nothing in the codebase can
#: issue a refund against either, so an offer of a third is a promise made to a
#: customer who may act on it.
_OTHER_PAYMENT = (
    "فيزا", "ڤيزا", "بالفيزا", "كارت", "credit card", "بطاقة",
    "انستاباي", "إنستاباي", "instapay", "فودافون كاش", "محفظة",
    "تحويل بنكي", "paypal", "باي بال", "لينك دفع", "payment link",
)

#: "Online" is deliberately not in that list, and this is the one entry worth
#: explaining. It used to be, and it made the gate fail the shop's own correct
#: answer twice over: this *is* an online shop and says so (the prompt's first
#: line calls it «محل هدوم أونلاين»), and paying online through the website is
#: a real option here. The prompt requires that sentence verbatim --
#: «بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع» -- since 43eb403, which
#: settled it after the prompt and `assistant/comment_faq.py` had been telling
#: customers different things depending on whether they asked in a DM or in a
#: comment. A gate that fails the answer the prompt mandates is testing the
#: wrong thing, so the list above is now only the methods the shop genuinely
#: cannot take.

#: ...but talking about cash on delivery is exactly right, and some of the
#: words above appear inside perfectly correct sentences ("مش بنقبل فيزا").
#: A denial is not an offer.
#: "عند الاستلام" is the cash-on-delivery phrase itself, and deliberately not
#: the bare word "كاش" -- that one is inside "فودافون كاش", which is a payment
#: method this shop really cannot take.
_PAYMENT_DENIAL = (
    "مش", "ما بنقبل", "مابنقبلش", "غير متاح", "بس كاش", "كاش بس", "only cash",
    "عند الاستلام",
)


def offers_another_payment_method(text: str) -> str:
    """The payment method a reply offered that this shop does not have, if any.

    Deliberately a keyword check rather than a model call: a judge that can be
    wrong is not a gate, and the failure being guarded against is specific and
    literal. A sentence that *denies* the method is left alone -- "مش بنقبل
    فيزا، كاش عند الاستلام بس" is the correct answer, not a violation.
    """
    lowered = (text or "").lower()
    clauses = re.split(r"[.\n،؛!?]", text or "")
    for method in _OTHER_PAYMENT:
        if method.lower() not in lowered:
            continue
        # Look at the clause it appears in, not the whole reply: a summary can
        # correctly say "cash on delivery" in one line and nothing about cards
        # in another.
        for clause in clauses:
            if method.lower() in clause.lower() and not any(
                d.lower() in clause.lower() for d in _PAYMENT_DENIAL
            ):
                return method

    return ""


#: The shop is called Wanas Gallery and the short form is Wanas. Both are
#: correct and nothing else is.
SHOP_NAME = frozenset({"Wanas", "WANAS"})

#: The one product name that legitimately contains the brand abbreviated.
#: Masked out before the scan, so a bare `WNS` elsewhere is still caught --
#: `WNS` used as a name for the shop *is* the misspelling this rule is for.
_BOXY_WNS_TEE = re.compile(r"\bBoxy\s+WNS\s+Tee\b", re.IGNORECASE)

#: A Latin word built on the brand's consonant skeleton -- w, then n, then s,
#: with only vowels between. Catches Wnas, Wans, WNS, Wanass and the lowercase
#: slug forms, and matches almost nothing else a reply from a clothes shop
#: contains.
_BRAND_SHAPED = re.compile(r"\b[Ww][AaEeIiOoUu]*[Nn][AaEeIiOoUu]*[Ss]{1,2}[A-Za-z]*\b")

#: Ordinary English words with the same skeleton. Short list on purpose: these
#: are the only ones plausible in a reply, and a gate that guessed more widely
#: would start excusing real misspellings.
_NOT_THE_BRAND = frozenset({"wins", "wines", "wanes"})


def misspelled_shop_name(text: str) -> list[str]:
    """Every spelling of the shop's name in `text` that is not how it is spelled.

    The brand is the one word in a reply the model cannot get away with
    reconstructing: a customer who is told the shop is called something it is
    not has been given wrong information about the thing they are buying from.
    It is also the word most exposed to reconstruction, because the model sees
    it in four surface forms -- `Wanas Gallery`, `WANAS Hoodie`, `Boxy WNS Tee`
    and the Arabic «ونس» -- and Arabic writes no short vowels, so the Arabic
    form is literally w-n-s.

    Latin only, deliberately. The Arabic «وناس» is an ordinary word ("and
    people") and a rule that flagged it would fail correct replies; the
    reported failure was Latin, and this is the half that can be checked
    without guessing.
    """
    masked = _BOXY_WNS_TEE.sub(" ", text or "")
    return [
        token
        for token in _BRAND_SHAPED.findall(masked)
        if token not in SHOP_NAME and token.lower() not in _NOT_THE_BRAND
    ]


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


#: A line's first strong character decides the direction of the whole line
#: (UAX #9, rule P2), so a line opening with a Latin word is laid out
#: left-to-right in the middle of a right-to-left message. A leading bullet
#: or dash is neutral and does not count, which is why it is stripped first.
#: A leading *digit* is fine and is deliberately not flagged: digits are not
#: strong, so «2 قطع» still takes its direction from the Arabic after it.
_LINE_LEAD = re.compile(r"^[\s\u2022*\u2013\u2014-]*(.)")

#: A Latin word, a neutral separator, then a digit -- with no Arabic in
#: between to anchor it. The neutral takes the paragraph's right-to-left
#: direction and the two swap, so «لون Black — 590 جنيه» is *displayed*
#: «لون 590 — Black جنيه»: the customer reads the price where the colour is.
#: A plain space is not a separator here and is not flagged -- «مقاس L 590»
#: lays out correctly, because a digit directly after a Latin letter takes
#: that letter's direction (rule W7).
_LATIN_THEN_NUMBER = re.compile(
    r"([A-Za-z][A-Za-z0-9]*)\s*([\u2014\u2013,\u060c:;-])\s*([0-9\u0660-\u0669\u06f0-\u06f9])"
)


#: The sleeve words, as a reply would write them. Deliberately the shop's own
#: Arabic plus the English the model falls back to -- this is matched against
#: what *went out*, not against what the customer typed.
_SLEEVE_WORDS = (
    "نص كم", "نُص كم", "نصف كم", "كم قصير", "كم طويل", "من غير كم", "بدون كم",
    "طول الكم", "half sleeve", "short sleeve", "long sleeve", "sleeveless",
)

#: "I don't have that information". Every one of these is a sentence the bot
#: has actually sent, and each of them is correct somewhere -- about a size
#: chart nobody published, about a colour the shop never made. Beside a sleeve
#: word it is none of those: sleeve length is a catalog field now, and a reply
#: that reaches for one of these instead of reading it has fallen back to the
#: exact answer that made the field necessary.
_NO_DATA = (
    "معنديش المعلومة", "معنديش معلومات", "معنديش بيانات", "مش متوفرة عندي",
    "مفيش معلومات", "مفيش بيانات", "مش موجودة عندي", "مش عارف",
    "no data", "no information", "not available", "don't have",
)


def dodged_a_sleeve_question(text: str) -> str:
    """The sentence a reply used to get out of answering about sleeve length.

    A customer asked for «البولو النص كم» and was told the shop has two polos
    and no published data about sleeve length for either, plus an offer to
    fetch a person -- about a polo that is on the shelf and is half-sleeve.
    Two holes behind it: «نص كم» was not in the catalog's vocabulary, and
    nothing recorded sleeve length at all. Both are closed
    (`domain/services/sleeves.py`), and this is the rule that keeps them
    closed: a reply that mentions sleeve length and pleads ignorance in the
    same breath is the failure coming back.

    Deliberately *not* a ban on "not recorded" in general. A product the shop
    genuinely has not filled in still has a null, and saying so is the right
    answer -- what this catches is saying it *and* offering a handoff, which
    is what turns an answerable question into somebody's afternoon.
    """
    lowered = (text or "").lower()
    if not any(word.lower() in lowered for word in _SLEEVE_WORDS):
        return ""
    excuse = next((phrase for phrase in _NO_DATA if phrase.lower() in lowered), "")
    if not excuse:
        return ""
    return excuse


def layout_problems(text: str) -> list[str]:
    """Every way a reply is laid out so the customer reads something other
    than what was written.

    Both of these are checked on the *stored* text, before `common/bidi.py`
    lays it out at the send boundary. That is deliberate twice over: the
    dashboard shows staff this exact string with no bidi pass on it, and text
    that needs no repair is better than text that got repaired -- every shape
    the prompt asks for renders correctly on its own, and every shape it
    forbids does not.
    """
    problems: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        lead = _LINE_LEAD.match(line)
        if lead and _LATIN_LETTER.match(lead.group(1)):
            problems.append(f"a line starts with a Latin word: {line.strip()[:48]!r}")
        hit = _LATIN_THEN_NUMBER.search(line)
        if hit:
            problems.append(
                f"{hit.group(1)!r} is followed straight by a number across "
                f"{hit.group(2)!r} -- they swap on the phone"
            )
    return problems

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

            offered = offers_another_payment_method(text)
            if offered:
                failures.append(
                    f"{where}: the reply offered {offered!r} -- this shop is "
                    "cash on delivery only"
                )

            if looks_truncated(text):
                failures.append(f"{where}: the reply reads as cut off")

            dodged = dodged_a_sleeve_question(text)
            if dodged:
                failures.append(
                    f"{where}: the reply talked about sleeve length and then said "
                    f"{dodged!r} -- sleeve length is a catalog field, read it"
                )

            misspelled = misspelled_shop_name(text)
            if misspelled:
                failures.append(
                    f"{where}: the reply spelled the shop's name "
                    f"{sorted(set(misspelled))} -- it is Wanas Gallery"
                )
            for problem in layout_problems(text):
                failures.append(f"{where}: {problem}")

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

        # Photos. `get_variants` is the only tool that attaches one, so any
        # change that lets the model answer a product question *without*
        # calling it can silently stop the customer ever seeing the garment --
        # a reply that is faster and worse in the one way a clothes shop
        # cannot afford. Compared as "the golden run sent pictures and this one
        # sent none", not as an exact count: how many colourways a reply shows
        # is a judgement the model is allowed to make differently.
        golden_photos = max((r.get("attachments") or 0) for r in golden_replies)
        new_photos = max((r.get("attachments") or 0) for r in fresh_replies)
        if golden_photos and not new_photos:
            failures.append(
                f"{where}: the golden run sent {golden_photos} photo(s) and this one sent none"
            )

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
            message = f"{where}: facts dropped from the reply: {sorted(lost)}"
            if allow_tool_drift:
                # A run that took a different route through the tools reaches a
                # differently-shaped reply, and "the sizing answer no longer
                # repeats the price" is phrasing, not a lost fact. With drift
                # allowed this is reported for a person to read rather than
                # failed on; `expects_text` is the part that still fails, and
                # it pins the facts the scenario actually cares about.
                print(f"  note: {message}")
            else:
                failures.append(message)

    # The part that is *not* negotiable, drift allowed or not: a conversation
    # the golden run consulted the catalog during must still consult it. A
    # reply that looks nothing up and still states a price is the invented-fact
    # failure every rule in this repository is built against, and it is exactly
    # what a latency change could buy by accident.
    #
    # Judged per **conversation**, not per message, and that is the whole
    # subtlety. Which step does the lookup is the model's business: putting the
    # piece in the cart on "size L in black" rather than waiting for "yes, add
    # it" is a better reply, not a worse one, and a per-step rule fails it --
    # it failed exactly that, on a change that does not touch the prompt, the
    # tools or the model. Answering the *whole conversation* without ever
    # looking anything up is a different thing entirely, and still fails.
    for scenario in sorted({key[0] for key in fresh_steps} | {key[0] for key in golden_steps}):
        golden_used = {
            tool
            for key, replies in golden_steps.items()
            if key[0] == scenario
            for reply in replies
            for tool in reply["tool_calls"]
        }
        new_used = {
            tool
            for key, replies in fresh_steps.items()
            if key[0] == scenario
            for reply in replies
            for tool in reply["tool_calls"]
        }
        if golden_used and not new_used:
            failures.append(
                f"{scenario}: the golden run looked something up ({sorted(golden_used)}) "
                "and this one answered the whole conversation without calling anything"
            )

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
