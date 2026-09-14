"""The gate that decides whether a speed-up is kept.

`scripts/quality_gate.py` is what let this repository's latency work proceed
without a person reading Egyptian Arabic after every change, so its rules are
load-bearing: a rule that stops firing turns the loop into one that keeps
whatever is fastest. Each test here is one rule, and each rule is a failure
that actually happened while the work was being done.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import quality_gate  # noqa: E402


def _reply(scenario, step, text, tools=(), photos=0, error=None, expects_tools=(), expects_text=()):
    return {
        "scenario": scenario,
        "step": step,
        "text": text,
        "tool_calls": list(tools),
        "attachments": photos,
        "error": error,
        "expects_tools": list(expects_tools),
        "expects_text": list(expects_text),
    }


def _run(*replies):
    return {"replies": list(replies)}


def check(golden, fresh, *, drift=True):
    return quality_gate.check(golden, fresh, allow_tool_drift=drift)


# --- cash on delivery -----------------------------------------------------
#
# The rule exists because a model measured as a possible swap offered online
# payment on its first run through the scenarios, and every other check in the
# gate passed it.


def test_offering_online_payment_fails():
    golden = _run(_reply("confirm_order", 2, "الإجمالي 650 جنيه كاش عند الاستلام", ("confirm_order",)))
    fresh = _run(
        _reply(
            "confirm_order",
            2,
            "الإجمالي 650 جنيه. بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع.",
            ("confirm_order",),
        )
    )
    failures = check(golden, fresh)
    assert any("cash on delivery only" in f for f in failures), failures


def test_saying_cash_on_delivery_is_not_offering_anything():
    reply = _reply("confirm_order", 2, "الإجمالي 650 جنيه كاش عند الاستلام. تحب أأكد؟", ("confirm_order",))
    assert check(_run(reply), _run(reply)) == []


def test_calling_the_shop_online_is_not_offering_online_payment():
    """This shop *is* an online shop and the system prompt's own first line
    says so. The bare word used to fail every reply that described the
    business -- including the greeting the prompt asks for."""
    assert quality_gate.offers_another_payment_method("إحنا محل هدوم أونلاين في مصر") == ""
    assert quality_gate.offers_another_payment_method("محل أونلاين والدفع كاش عند الاستلام") == ""


def test_online_next_to_paying_is_still_an_offer():
    """The verb can govern from an earlier clause of the same sentence."""
    assert quality_gate.offers_another_payment_method("تقدر تدفع أونلاين") != ""
    assert (
        quality_gate.offers_another_payment_method(
            "بتقدر تدفع كاش عند الاستلام، أو أونلاين من الموقع."
        )
        != ""
    )


def test_refusing_a_card_is_not_offering_one():
    """"مش بنقبل فيزا، كاش عند الاستلام بس" is the correct answer to "بتقبلوا
    فيزا؟", and a rule that fails it would push the model towards saying
    nothing at all."""
    assert quality_gate.offers_another_payment_method("مش بنقبل فيزا، كاش عند الاستلام بس") == ""
    assert quality_gate.offers_another_payment_method("تقدر تحول على انستاباي") != ""


# --- photographs ----------------------------------------------------------


def test_sending_no_photos_where_the_golden_run_sent_some_fails():
    golden = _run(_reply("sizes", 0, "المقاسات M و L و XL", ("get_variants",), photos=2))
    fresh = _run(_reply("sizes", 0, "المقاسات M و L و XL", ("get_products",), photos=0))
    failures = check(golden, fresh)
    assert any("sent none" in f for f in failures), failures


def test_sending_fewer_photos_is_a_judgement_not_a_regression():
    golden = _run(_reply("sizes", 0, "المقاسات M و L و XL", ("get_variants",), photos=4))
    fresh = _run(_reply("sizes", 0, "المقاسات M و L و XL", ("get_variants",), photos=1))
    assert check(golden, fresh) == []


# --- looking things up ----------------------------------------------------


def test_answering_a_whole_conversation_from_nothing_fails():
    golden = _run(_reply("sizes", 0, "المقاسات M و L بـ 590 جنيه", ("get_products",)))
    fresh = _run(_reply("sizes", 0, "المقاسات M و L بـ 590 جنيه", ()))
    failures = check(golden, fresh)
    assert any("without calling anything" in f for f in failures), failures


def test_a_lookup_that_moved_to_another_message_is_fine():
    """Putting the piece in the cart on "size L in black" rather than waiting
    for "yes, add it" is a better reply, not a worse one. Judged per
    conversation for exactly this reason."""
    golden = _run(
        _reply("add_to_cart", 0, "متوفر بـ 590 جنيه", ("get_products",)),
        _reply("add_to_cart", 1, "تمام، اتحط", ()),
    )
    fresh = _run(
        _reply("add_to_cart", 0, "متوفر بـ 590 جنيه، ضفتهولك", ()),
        _reply("add_to_cart", 1, "تمام، هو في الشنطة", ("get_products",)),
    )
    assert check(golden, fresh) == []


# --- the rest -------------------------------------------------------------


def test_a_fallback_reply_fails():
    from assistant import agent

    golden = _run(_reply("greeting", 0, "أهلاً بيك، تحب أساعدك في إيه؟"))
    fresh = _run(_reply("greeting", 0, agent.GENERIC_FAILURE))
    failures = check(golden, fresh)
    assert any("fallback" in f for f in failures), failures


def test_a_templated_fallback_is_recognised_too():
    """Two of the fallbacks carry a `{product}` placeholder, so matching the
    whole constant would never recognise the ones a long conversation actually
    reaches."""
    from assistant import agent

    filled = agent.PROMISE_FALLBACK_WITH_PRODUCT.format(product="Boxy WNS Tee")
    golden = _run(_reply("sizes", 0, "المقاسات M و L", ("get_variants",)))
    fresh = _run(_reply("sizes", 0, filled, ("get_variants",)))
    failures = check(golden, fresh)
    assert any("fallback" in f for f in failures), failures


def test_an_errored_turn_fails():
    golden = _run(_reply("greeting", 0, "أهلاً بيك"))
    fresh = _run(_reply("greeting", 0, "", error="provider_crash"))
    assert any("errored" in f for f in check(golden, fresh))


def test_a_reply_that_stopped_being_egyptian_fails():
    golden = _run(_reply("greeting", 0, "أهلاً بيك، تحب أساعدك في إيه؟"))
    fresh = _run(_reply("greeting", 0, "Hello, how can I help you today?"))
    assert any("Arabic" in f or "Latin" in f for f in check(golden, fresh))


def test_a_declared_fact_that_disappeared_fails():
    """`expects_text` is the part that still fails when tool drift is allowed:
    it pins the facts the scenario is actually about."""
    golden = _run(_reply("sizes", 0, "متاح مقاس L بـ 590 جنيه", ("get_variants",)))
    fresh = _run(
        _reply("sizes", 0, "متاح عندنا دلوقتي، تحب تشوف؟", ("get_variants",), expects_text=("590",))
    )
    assert any("590" in f for f in check(golden, fresh))


def test_a_silent_turn_is_not_judged_on_its_words():
    """`confirm_order` ends the turn and sends the confirmation itself, so an
    empty reply there is correct silence rather than a broken one."""
    golden = _run(_reply("confirm_order", 2, "", ("confirm_order",)))
    fresh = _run(_reply("confirm_order", 2, "", ("confirm_order",)))
    assert check(golden, fresh) == []


# --- the shop's own name --------------------------------------------------
#
# The model meets the brand in four surface forms -- `Wanas Gallery`, `WANAS
# Hoodie`, `Boxy WNS Tee` and the Arabic «ونس» -- and Arabic writes no short
# vowels, so the Arabic form is literally w-n-s. That is the whole mechanism
# behind a reply that calls the shop `Wnas` or `WNS`.


def test_the_shop_name_spelled_correctly_passes():
    golden = _run(_reply("greeting", 0, "أهلاً بيك في Wanas Gallery، محل هدوم أونلاين في مصر. تحب أساعدك في إيه؟"))
    fresh = _run(_reply("greeting", 0, "أهلاً بيك في Wanas Gallery، محل هدوم أونلاين في مصر. تحب أساعدك في إيه؟"))
    assert check(golden, fresh) == []


def test_the_short_form_is_also_correct():
    golden = _run(_reply("greeting", 0, "أهلاً بيك"))
    fresh = _run(_reply("greeting", 0, "دي من ماركة Wanas، وعندنا منها كل المقاسات. تحب تشوفها؟"))
    assert not [f for f in check(golden, fresh) if "shop's name" in f]


def test_the_product_name_prefix_is_not_a_misspelling():
    """`WANAS Hoodie` is the product's real name and is on the label."""
    golden = _run(_reply("sizes", 0, "هودي WANAS Hoodie متوفر عندنا دلوقتي بكل المقاسات"))
    fresh = _run(_reply("sizes", 0, "هودي WANAS Hoodie متوفر عندنا دلوقتي بمقاس L وكمان مقاس M"))
    assert not [f for f in check(golden, fresh) if "shop's name" in f]


def test_boxy_wns_tee_is_a_product_not_a_misspelling():
    """The one product name that legitimately contains the brand abbreviated.
    It is masked out before the scan; a bare `WNS` anywhere else is not."""
    golden = _run(_reply("product_question", 0, "عندنا دلوقتي تيشيرت Boxy WNS Tee بسعر كويس قوي"))
    fresh = _run(_reply("product_question", 0, "دي اللي عندنا دلوقتي: • تيشيرت Boxy WNS Tee — 450 جنيه. تحب تشوف حاجة تانية؟"))
    assert not [f for f in check(golden, fresh) if "shop's name" in f]


def test_wns_offered_as_the_shop_name_fails():
    """The failure the rule is named for: the abbreviation floating free of the
    product name and being used as the shop."""
    golden = _run(_reply("greeting", 0, "أهلاً بيك في Wanas Gallery، محل هدوم أونلاين في مصر. تحب أساعدك في إيه؟"))
    fresh = _run(_reply("greeting", 0, "أهلاً بيك في WNS، محل هدوم أونلاين في مصر. تحب أساعدك في إيه؟"))
    assert any("shop's name" in f and "WNS" in f for f in check(golden, fresh))


def test_the_vowelless_transliteration_fails():
    """`Wnas` is «ونس» read back letter by letter."""
    golden = _run(_reply("greeting", 0, "أهلاً بيك في Wanas Gallery، محل هدوم أونلاين في مصر. تحب أساعدك في إيه؟"))
    fresh = _run(_reply("greeting", 0, "أهلاً بيك في Wnas Gallery، محل هدوم أونلاين في مصر. تحب أساعدك في إيه؟"))
    assert any("Wnas" in f for f in check(golden, fresh))


def test_a_reordered_transliteration_fails():
    golden = _run(_reply("greeting", 0, "أهلاً بيك في Wanas Gallery، محل هدوم أونلاين في مصر. تحب أساعدك في إيه؟"))
    fresh = _run(_reply("greeting", 0, "أهلاً بيك في Wans Gallery، محل هدوم أونلاين في مصر. تحب أساعدك في إيه؟"))
    assert any("Wans" in f for f in check(golden, fresh))


def test_a_leaked_product_slug_fails():
    """A slug in a reply is the model reading tool arguments back to the
    customer, and it carries the brand lowercased."""
    golden = _run(_reply("product_question", 0, "عندنا دلوقتي هودي WANAS Hoodie بكل المقاسات المتاحة"))
    fresh = _run(_reply("product_question", 0, "المنتج wanas-hoodie متاح عندنا دلوقتي بكل المقاسات"))
    assert any("shop's name" in f for f in check(golden, fresh))


def test_an_ordinary_english_word_is_not_the_brand():
    """`wins` has the same consonant skeleton and is not a misspelling."""
    assert quality_gate.misspelled_shop_name("he wins") == []
    assert quality_gate.misspelled_shop_name("Wanas Gallery") == []
