"""The bot's Egyptian Arabic: what code guarantees about it.

«عامل ايه» was answered «كل تمام» -- broken Arabic, not a typo a customer
forgives. Measured live against the real prompt and model, the same class of
slip kept coming back: «العفى», «تقلي», a Levantine «وين», an MSA «لدينا», a
staff name nobody has («معك أحمد»), and the internal word «reference». Three
causes, each pinned here:

* the model was thinking for zero tokens (`OPENROUTER_REASONING_EFFORT` defaulted
  to "low", which on this model is none at all) -- now "medium";
* the prompt carried Persian letters, forbade answering «عامل ايه» at all, and
  taught «reference» as a word to say;
* nothing below the model caught a slip with one right answer (fixed in
  `reply_rules.correct`) or a word that is not Egyptian (regenerated through
  `reply_rules.violation`).
"""

from __future__ import annotations

import re

import pytest

from assistant import agent, customer_name, reply_rules
from assistant.prompt import SYSTEM_PROMPT
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from config.settings import load_settings

CHANNEL = "whatsapp"
WHO = "201000000088"


# --------------------------------------------------------------------------
# the slips with one right answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wrong", "right"),
    [
        ("الحمد لله، كل تمام", "الحمد لله، كله تمام"),
        ("وكل تمام الحمد لله", "وكله تمام الحمد لله"),
        ("كل تمام؟", "كله تمام؟"),
        ("العفى، تحت أمرك", "العفو، تحت أمرك"),
        ("ولو تقلي مقاسك", "ولو تقولي مقاسك"),
        ("أبراهم:", "أبرزهم:"),
        ("إنشاء الله يوصلك بكرة", "إن شاء الله يوصلك بكرة"),
        ("أحد من الفريق هيكلمك", "حد من الفريق هيكلمك"),
        ("متنادیش", "متناديش"),  # a Persian ی
    ],
)
def test_a_slip_with_one_right_answer_is_fixed(wrong, right):
    fixed, fixes = reply_rules.fix_arabic(wrong)
    assert fixed == right
    assert fixes


@pytest.mark.parametrize(
    "fine",
    ["كل حاجة تمام", "كلكم تمام", "كل تماماً", "العفو", "تقولي", "يوم الأحد"],
)
def test_correct_arabic_is_left_alone(fine):
    assert reply_rules.fix_arabic(fine) == (fine, [])


def test_the_internal_word_for_the_order_number_never_reaches_a_customer():
    fixed, _ = reply_rules.fix_arabic("ابعتلي رقم الأوردر (الـ reference) عشان أتابعه")
    assert fixed == "ابعتلي رقم الأوردر عشان أتابعه"
    fixed, _ = reply_rules.fix_arabic("ابعتلي الـ reference بتاعك")
    assert fixed == "ابعتلي رقم الأوردر بتاعك"


def test_correct_applies_the_arabic_fixes():
    text, fixes = reply_rules.correct(
        "الحمد لله، كل تمام", vocabulary=[], references={}, states_money=False
    )
    assert text == "الحمد لله، كله تمام"
    assert "كل تمام -> كله تمام" in fixes


# --------------------------------------------------------------------------
# words that are not Egyptian
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "الخروج بتكون وين؟",
        "شو بتحب؟",
        "لدينا تيشيرتات كتير",
        "ابعتلي رقم الأوردر تبعك",
        "هل ترغب في إضافته؟",
        "سوف نرسل لك الأوردر",
    ],
)
def test_a_word_that_is_not_egyptian_sends_the_reply_back(reply):
    assert reply_rules.not_egyptian(reply)
    assert reply_rules.violation(reply, customer="x", previous="", results=[]).startswith(
        "dialect: "
    )


@pytest.mark.parametrize(
    "reply",
    ["الأوردر فين؟", "عندنا تيشيرتات كتير", "تحب أضيفه؟", "رقم الأوردر بتاعك", "هنبعتلك الأوردر"],
)
def test_egyptian_passes(reply):
    assert reply_rules.not_egyptian(reply) == ""


def test_a_customer_is_never_sent_the_broken_phrase(seeded):
    """The complaint, end to end: the model writes «كل تمام», the customer
    reads «كله تمام»."""
    provider = ScriptedProvider([ModelReply(text="الحمد لله، كل تمام. حضرتك عامل إيه؟")])
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عامل ايه", provider=provider)
    assert reply.text == "الحمد لله، كله تمام. حضرتك عامل إيه؟"


def test_a_levantine_reply_is_written_again(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(text="تمام، الخروج بتكون وين؟"),
            ModelReply(text="تمام، الخروج هيكون فين؟"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "عايز حاجة للخروج", provider=provider)
    assert reply.text == "تمام، الخروج هيكون فين؟"
    # And the second attempt was told why.
    assert "مش مصري" in provider.calls[1][0]


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------


def test_the_prompt_carries_no_persian_letters():
    """A model copies the letters it is shown; «متنادیش» used a Persian ی."""
    assert not re.search("[یکھ]", SYSTEM_PROMPT)
    for note in (customer_name._ASK_NOTE, customer_name._KNOWN_NOTE):
        assert not re.search("[یکھ]", note)


def test_the_prompt_lets_the_bot_answer_how_it_is():
    """It used to forbid talking about its own state at all, so «عامل ايه»
    got no answer, or a broken one."""
    assert "كله تمام" in SYSTEM_PROMPT
    assert "«الدنيا تمام»" not in SYSTEM_PROMPT


def test_the_prompt_names_the_dialect_and_forbids_a_persona():
    assert "«فين» مش «وين»" in SYSTEM_PROMPT
    assert "مالكش اسم شخصي" in SYSTEM_PROMPT


def test_the_prompt_no_longer_teaches_the_word_reference():
    assert "قول الـ `reference`" not in SYSTEM_PROMPT
    assert "«رقم الأوردر»" in SYSTEM_PROMPT


def test_the_name_question_does_not_script_a_greeting_nobody_made():
    """«ازيك عامل ايه» was answered «وعليكم السلام» -- the model copied the
    example it was given for asking the name."""
    assert "وعليكم السلام" not in customer_name._ASK_NOTE


# --------------------------------------------------------------------------
# the model's thinking budget
# --------------------------------------------------------------------------


def test_the_model_thinks_by_default(monkeypatch):
    monkeypatch.delenv("OPENROUTER_REASONING_EFFORT", raising=False)
    assert load_settings().openrouter_reasoning_effort == "medium"
