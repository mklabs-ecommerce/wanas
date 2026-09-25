"""Did the speed-up cost an answer?

    python scripts/quality_gate.py --record docs/golden.json     # before
    python scripts/quality_gate.py --check  docs/golden.json     # after every change

Runs the same conversations `bench_turn.py` times (`scripts/bench_scenarios.py`)
and judges the replies against a recorded golden set. This is the rule that
lets an optimisation be kept or reverted without anyone reading the Arabic:
a change that makes the bot faster and slightly wrong is not a speed-up, and
"slightly wrong" in a shop means a price, a size or a stock claim.

Eighteen checks, and each one is a failure mode this repository has already
paid for at least once:

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
8. **And no product name it made up.** The brand rule above is one word; this
   is the other class of the same failure. A real conversation sold a
   `Lightweight Sweatpant` as a **Lightwelson** Sweatpant -- six times, in
   every message that mentioned it, with the price and the colours correct
   beside it. A product name arrives in a tool result and has to come out byte
   for byte, so a Latin word that is a near-miss of a catalog word is
   reconstruction from memory and nothing else.
9. **It does not tell a customer the shop has no such line without asking.**
   «مفيش قسم حريمي» went out with no tool called in the turn, to a shop that
   has a women's department. A price answered from memory is checked at the
   door; a customer told their whole category does not exist here just leaves.
10. **It does not say the last reply again.** A reply that repeats the one
   before it has not used the message in between. The audited conversation
   listed two sweatpants and asked "photos, or sizes?"; the customer answered
   «الاتنين» and got the same two lines back with "which of the two?" under
   them.
11. **Laid out so it reads the way it was written.** Arabic with Latin and
   numbers inside it is the normal case here, and two shapes of line come
   out reordered on the phone: one opening with a Latin word takes
   left-to-right direction for the whole line, and a number separated from
   a Latin word by a dash or a comma swaps places with it -- «لون Black —
   590 جنيه» is displayed «لون 590 — Black جنيه». `common/bidi.py` repairs
   both at the send boundary, and this rule keeps them from being written
   in the first place: the dashboard shows staff the unrepaired string, and
   every shape the prompt asks for already renders correctly on its own.
12. **A sleeve question is answered, not deflected -- ever.** `Product.sleeve`
   is total: half, long or sleeveless, for every product. So beside the words
   «نص كم», *any* profession of ignorance fails -- «مش متسجّل» included, which
   this rule used to allow because it used to be true. It is not reachable any
   more, and a reply that produces it has invented a state the catalog does
   not have.
13. **Photographs still go out.** Judged against the golden run: sending fewer
   is a judgement, sending none is a regression. And judged absolutely as well,
   on the steps whose reply is about one named garment -- a comparison rule
   cannot catch a shop that has *never* sent a photo, and for a long time this
   one had not: `get_products` attached nothing, so every answer that came out
   of a search arrived as text.
14. **And the size chart does not.** Not unless the customer asked about sizes,
   measurements or fit. It used to ride along with every `get_variants` call
   for a product that has a chart, so a question about price or colour was
   answered with a measurements table -- which got worse the moment a product
   reply started carrying its photo, because that made the call the ordinary
   case rather than the rare one.
15. **And a reply never claims a photograph it is not sending.** «دي صورتهم
   الاتنين 👆» went out beside one picture, then beside none, then beside none
   again, in one conversation -- the words are composed by the model and the
   pictures are attached by the tool layer, and nothing used to join them back
   together before the reply left. A customer reading a message that says a
   photo is there has been told something false about their own screen.
16. **And it never hands our failure to the customer to debug.** That same
   conversation ended with «ممكن تكون مشكلة في النت أو التطبيق — جرب اقفل
   الواتس وافتحه تاني», about a photograph the system had already recorded as
   refused. The picture leaves from here: if it did not arrive, the failure is
   ours, and a customer saying so is ground truth.
17. **One photo per product in a reply that shows several**, unless the
   customer asked for that product's colours. Asked "photos of which of the
   two?", the customer answered «الاتنين» and received every colourway of both
   -- a screenful of notifications in answer to a two-word message that asked
   for two pictures. A reply about *one* product may show up to
   `showcase.FIRST_SHOWING_PHOTOS` of it: that is the first showing
   `assistant/showcase.py` makes on purpose, and never more.
18. **And it answers the garment that was asked for.** «فيه قمصان» came back
   «أيوه، عندنا تيشيرتات كتير» with four t-shirts under it. In Egyptian a قميص
   is a button-up shirt, a تيشيرت is not one, and this shop sells no shirts --
   so that is the shop saying yes to something it does not have. Rule 9 with
   the sign flipped: that one denies what the shop has, this one affirms what
   it does not. Offering the tees is right; offering them without first saying
   we have no shirts is what makes it a wrong answer.

19. **An add is filed as an add.** A customer asking for a piece *as well as*
   what they already ordered must produce an `item_add`, never an `item_swap`.
   For a while `item_swap` was the only post-order request type there was, so
   «ينفع اضيفه علي نفس الاوردر اللي فات» was filed as "remove the Knitted Polo
   (Olive, XL), put the Heart Top (Black, S) in its place" -- against a line
   the customer had never mentioned and still wanted, one staff click from
   leaving his order. The reverse fails too: a genuine swap filed as an add
   leaves the old garment on the order and bills for both.
20. **And the reply says what was filed.** The same conversation answered
   «عشان نضيف عليه قطعة جديدة، فبعتلهم الطلب» while a swap sat in the queue.
   The words are the model's and the filing is the tool layer's, so the two
   are checked against each other -- `assistant/order_change_claims.py`, the
   same rule the turn itself enforces before sending. A reply that describes
   an action the queue does not hold is worse than no reply: the customer has
   been told, in writing, that the thing about to happen to their order is
   not the thing about to happen to their order.
21. **A size chart or product photos, not both.** «When I ask for the size
   chart it sends the chart together with product photos» -- it did, by
   design, and a table between four pictures of a T-shirt is a table nobody
   finds. Both only when the customer's own message asked for both.

Exit code 0 means keep the change; 1 means revert it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from assistant import agent, order_change_claims, photo_claims, showcase  # noqa: E402
from assistant.providers import set_provider  # noqa: E402
from assistant.reply_rules import (  # noqa: E402 -- one definition, run live too
    _REPEAT_RATIO,
    catalog_vocabulary,
    denies_a_whole_section,
    dodged_a_sleeve_question,
    garbled_catalog_words,
    misspelled_shop_name,
    offered_a_garment_they_did_not_ask_for,
    offers_another_payment_method,
    repeats_the_previous_reply,
)
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
    agent.PARTIAL_IMAGE_FALLBACK,
    agent.BLAME_FALLBACK,
    agent.CHANGE_FALLBACK,
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


#: How many garment photos one product may appear in, in a reply that shows
#: several products, unless the customer asked for its colours. One. See
#: `tools.base.MAX_PRODUCT_IMAGES` -- this is the same rule judged from the
#: outside, on what actually went out. A reply about one product alone may show
#: `showcase.FIRST_SHOWING_PHOTOS` of it.
_PHOTOS_PER_PRODUCT = 1


def claimed_a_photo_it_did_not_send(text: str, photos: int) -> str:
    """A reply whose words claim a picture the reply is not carrying.

    The audited conversation, three messages running: «دي صورتهم الاتنين 👆»
    with one photo attached and one refused by the platform, then «دي صورة
    Lightweight الأسود 👆» with none at all, then «دي صور الاتنين تاني 👆». The
    customer said each time that nothing had arrived, and was right each time.

    Judged here on the count alone -- the per-product form of the same rule
    lives in `assistant/photo_claims.py` and runs inside the turn, where the
    labels are. What a gate can check without them is the case that cannot be
    argued with: the sentence says a picture is coming and nothing went.
    """
    if photos > 0 or not photo_claims.promises_images(text):
        return ""
    return (text or "").strip()[:60]


def too_many_photos_of_one_product(counts: dict, asked_for_colors: bool) -> str:
    """A reply that answered "show me both" with a gallery of each.

    «الاتنين» -- both -- is a request for two photographs, one per product. It
    produced every colourway of both, because `more_images` is one flag meaning
    two different things and nothing counted pictures across the turn. A
    screenful of notifications for a two-word message is not a better answer
    than two pictures; it is the shop not having listened.

    The exemption is the request the gallery exists for: a customer who asked
    for the colours gets the colours.
    """
    if asked_for_colors:
        return ""
    counts = counts or {}
    limit = showcase.FIRST_SHOWING_PHOTOS if len(counts) == 1 else _PHOTOS_PER_PRODUCT
    over = sorted(
        f"{product or 'unnamed'}={count}" for product, count in counts.items() if count > limit
    )
    return ", ".join(over)


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
        vocabulary = catalog_vocabulary(db)
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
                reply["expects_photo"] = bool(step.expects_photo)
                reply["sizing_question"] = bool(step.sizing_question)
                reply["asked_for_colors"] = bool(step.asked_for_colors)
                reply["asked_for_photos"] = bool(step.asked_for_photos)
                reply["expects_filed"] = step.expects_filed
                # What the customer actually typed. The vocabulary rule below
                # is about the gap between the question and the answer -- a
                # customer asking for a garment this shop does not sell -- and
                # it cannot be judged from the reply alone.
                reply["customer_text"] = step.text
                replies.append(reply)
    return {"facts": facts, "vocabulary": vocabulary, "replies": replies}


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

    #: The previous step's reply in each scenario, for the repeat rule below.
    previous_text: dict[str, str] = {}

    for key, fresh_replies in sorted(fresh_steps.items()):
        where = f"{key[0]}[{key[1]}]"
        golden_replies = golden_steps.get(key) or []

        said_before = previous_text.get(key[0], "")
        latest = next((r.get("text") or "" for r in reversed(fresh_replies) if r.get("text")), "")
        if latest:
            previous_text[key[0]] = latest

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

            # Rule 19: what the customer asked for is what went in the queue.
            filed_kinds = list(reply.get("filed") or [])
            wanted = reply.get("expects_filed") or ""
            if wanted and filed_kinds != [wanted]:
                failures.append(
                    f"{where}: the customer asked for {wanted} and the queue got "
                    f"{filed_kinds or ['nothing']}"
                )

            # Rule 20: and the reply describes that and not the other one.
            said_wrong = order_change_claims.mismatch(
                text, order_change_claims.filed_kind(filed_kinds)
            )
            if said_wrong:
                failures.append(f"{where}: the reply describes the wrong request ({said_wrong})")

            # Judged on its own terms, not against the golden run. The
            # golden-comparison rule further down catches a *regression* in
            # photographs; it cannot catch a shop that has never sent one,
            # and for most of this set's history it had not -- `get_products`
            # attached nothing, so a product answered from a search arrived as
            # text. Four of five live product conversations carried no picture.
            photos_out = (reply.get("attachments") or 0) - (reply.get("charts") or 0)
            if reply.get("expects_photo") and photos_out <= 0:
                failures.append(
                    f"{where}: the reply is about one garment and sent no photo of it"
                )

            # The opposite failure, and the reason the count above subtracts.
            # The size chart used to ride along with every `get_variants` call
            # for a product that has one, so a question about price, colour or
            # shipping came back with a measurements table nobody asked for.
            if (reply.get("charts") or 0) and not reply.get("sizing_question"):
                failures.append(
                    f"{where}: a size chart went out and the customer never asked "
                    "about sizes, measurements or fit"
                )

            # Rule 21: and never both. A sizing question answered with the
            # chart *and* four photos of the garment buries the table; a
            # product question answered with a chart is rule 14 above.
            if (reply.get("charts") or 0) and photos_out > 0 and not reply.get("asked_for_photos"):
                failures.append(
                    f"{where}: a size chart and product photos went out together, "
                    "and the customer asked for one of them"
                )

            # One product, one photo -- unless the customer asked for the
            # colours, which is the only request a gallery answers.
            gallery = too_many_photos_of_one_product(
                reply.get("photos_by_product") or {}, bool(reply.get("asked_for_colors"))
            )
            if gallery:
                failures.append(
                    f"{where}: more than one photo of the same product went out "
                    f"({gallery}) and the customer never asked for its colours"
                )

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

            claimed = claimed_a_photo_it_did_not_send(text, photos_out)
            if claimed:
                failures.append(
                    f"{where}: the reply says a photo is on its way and none went "
                    f"out: {claimed!r}"
                )

            blamed = photo_claims.blames_the_customer(text)
            if blamed:
                failures.append(
                    f"{where}: the reply blamed the customer's own phone or "
                    f"connection ({blamed!r}) for a photo that did not arrive -- "
                    "the picture leaves from here, so the failure is ours"
                )

            dodged = dodged_a_sleeve_question(text)
            if dodged:
                failures.append(
                    f"{where}: the reply talked about sleeve length and then said "
                    f"{dodged!r} -- sleeve length is a catalog field, read it"
                )

            garbled = garbled_catalog_words(text, fresh.get("vocabulary") or [])
            if garbled:
                failures.append(
                    f"{where}: the reply named something the catalog does not have: "
                    f"{', '.join(garbled)} -- a product name is quoted, not composed"
                )

            misspelled = misspelled_shop_name(text)
            if misspelled:
                failures.append(
                    f"{where}: the reply spelled the shop's name "
                    f"{sorted(set(misspelled))} -- it is Wanas Gallery"
                )
            renamed = offered_a_garment_they_did_not_ask_for(
                reply.get("customer_text") or "", text
            )
            if renamed:
                failures.append(
                    f"{where}: the customer asked for a garment this shop does not "
                    f"sell and the reply {renamed} -- the nearest thing renamed is "
                    "not an answer"
                )

            denial = denies_a_whole_section(text)
            if denial and not reply["tool_calls"]:
                failures.append(
                    f"{where}: the reply said {denial!r} without looking anything up "
                    "-- what this shop stocks is not something to answer from memory"
                )

            ratio = repeats_the_previous_reply(text, said_before)
            if ratio >= _REPEAT_RATIO:
                failures.append(
                    f"{where}: the reply is {ratio:.0%} the previous one said again -- "
                    "the customer's message in between changed nothing"
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
