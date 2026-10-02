"""The prompt audit of 2026-10-02 (`docs/prompt_review.md`), pinned.

Each test is one finding: a Wanas-era product name or category in the prompt,
a rule that contradicted the tool behind it, a customer-facing line written
for a man only, or a promise of a handoff nobody made.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from assistant import action_claims, agent, interactive
from assistant.prompt import _EXAMPLE_PRICE, SYSTEM_PROMPT
from assistant.tools.support_tools import MODEL_HANDOFF_REASONS
from domain.services import notifications

SEED = json.loads(Path("data/products_seed.json").read_text(encoding="utf-8"))
NAMES = {p["name"] for p in SEED}


def test_every_product_the_prompt_names_is_one_the_shop_sells():
    """«Rehla Backless Top» was the layout example; the product is the
    «Rehla V-Halter Neck Backless Top». A model copies the examples it is
    shown, and the rule two lines above says to copy names exactly."""
    named = set(re.findall(r"Rehla(?: [A-Z][\w-]*)+", SYSTEM_PROMPT))
    # «Rehla Squared Top» is named only as the wrong spelling to avoid.
    unknown = named - NAMES - {"Rehla Squared Top"}
    assert not unknown, unknown


def test_the_layout_example_price_is_that_products_price():
    tops = next(p for p in SEED if p["name"] == "Rehla Tops")
    assert float(tops["price"]) == _EXAMPLE_PRICE
    assert f"السعر {_EXAMPLE_PRICE} جنيه" in SYSTEM_PROMPT


def test_the_prompt_does_not_hand_the_model_a_category_menu():
    """The greeting «توب، تيشيرت، بنطلون، هودي ولا كاب؟» came from a category
    list in the prompt's first line -- every hoodie and cap is sold out live."""
    opening = SYSTEM_PROMPT.split("\n\n")[0]
    for word in ("هوديز", "كابات", "تيشيرتات", "بنطلونات"):
        assert word not in opening
    assert "متعرضش في الترحيب قايمة أقسام ولا منتجات" in SYSTEM_PROMPT


def test_the_handoff_rule_matches_what_request_human_does():
    """The tool ends the turn on the shop's own sentence; the prompt used to
    ask the model to write that sentence itself, which is never sent."""
    section = SYSTEM_PROMPT.split("# التحويل لموظف")[1]
    assert "request_human بيقفل الدور" in section
    for reason in re.findall(r"\b(complaint|customer_asked|size_help|unclear|out_of_scope)\b", SYSTEM_PROMPT):
        assert reason in MODEL_HANDOFF_REASONS


def test_a_return_request_names_its_handoff_reason():
    section = SYSTEM_PROMPT.split("# الاستبدال والإلغاء والمرتجع")[1]
    assert "complaint" in section and "customer_asked" in section
    # A post-delivery exchange goes to the team, not to request_item_swap
    # (that is the pre-shipping change) -- the two used to sit side by side.
    assert "request_item_swap" not in section


def test_a_complaint_is_handed_off_without_asking_for_details_first():
    section = SYSTEM_PROMPT.split("# التحويل لموظف")[1]
    assert "من غير ما تطلب رقم أوردر" in section


def test_gender_adapts_and_never_defaults_to_masculine():
    assert "متفترضش أبداً الزبون ولد ولا بنت" in SYSTEM_PROMPT
    assert "«تحبي» لبنت، «تحب» لولد" in SYSTEM_PROMPT
    assert "ممنوع صيغة المذكر كإنها الأصل" in SYSTEM_PROMPT
    assert "«آسفين»" in SYSTEM_PROMPT


def test_the_prompt_covers_the_safety_cases():
    section = SYSTEM_PROMPT.split("# انت بتتكلم في حاجة واحدة بس")[1]
    assert "براند أو محل تاني" in section
    assert "رقم قومي" in section
    assert "شتيمة" in section


def test_the_payment_nudge_offers_nothing_but_cash_on_delivery():
    assert "أونلاين" not in agent._RULE_NUDGE["payment"]


#: Second-person forms written for a man only. Every canned line a customer
#: reads is neutral («حضرتك», a question with no verb to inflect).
_MASCULINE = re.compile(r"(?<!\w)(?:تحب|عايز|عايزه|قولي|قوللي|ابعتلي|جرب|اختار|تقيّم|محتاج)(?!\w)")


def test_no_canned_line_is_written_for_a_man_only():
    lines = [
        agent.GENERIC_FAILURE, agent.RATE_LIMITED, agent.TRUNCATED_FALLBACK,
        agent.PROMISE_FALLBACK, agent.PROMISE_FALLBACK_WITH_PRODUCT,
        agent.PROMISE_FALLBACK_WITH_COLOR, agent.IMAGE_PROMISE_FALLBACK,
        agent.PARTIAL_IMAGE_FALLBACK, agent.BLAME_FALLBACK, agent.CHANGE_FALLBACK,
        *action_claims.FALLBACKS.values(),
        notifications.FEEDBACK_REQUEST_TEXT, notifications.HANDOFF_CLOSED_TEXT,
        interactive.REGION_PROMPT, interactive.GOVERNORATE_PROMPT, interactive.PICK_BUTTON,
    ]
    offenders = [line for line in lines if _MASCULINE.search(line)]
    assert not offenders, offenders


def check(text, results=()):
    return action_claims.unbacked(
        text, list(results), cart_has_items=lambda: False, has_orders=lambda: True
    )


def test_a_handoff_promised_without_request_human_is_caught():
    """Demo, 2026-10-02: «فهحول حضرتك لحد من الفريق» with no call -- the
    conversation was never paused and nobody was told."""
    assert check("البنطلون ده مفيش منه جدول، فهحول حضرتك لحد من الفريق يساعدك.") == "handoff"
    assert check("حوّلنا طلبك للفريق") == "handoff"


def test_an_offer_an_honest_no_or_a_filed_request_is_not_a_handoff_claim():
    assert check("نحوّل حضرتك لحد من الفريق؟") == ""
    assert check("مش هحولك دلوقتي") == ""
    assert check("حد من الفريق هيأكدلك.") == ""
    assert check("متعرضش تحويل بنكي") == ""
    assert check("The courier will hand you the parcel") == ""
    assert check("هحولك", [("request_human", {"queued": True})]) == ""
    assert check("حد من الفريق هيتواصل", [("request_item_swap", {"queued": True})]) == ""


def test_the_jacket_is_seeded_at_the_live_price():
    jacket = next(p for p in SEED if p["product_id"] == "rehlaa-jacket")
    assert jacket["price"] == 1000.0
    assert {v["price"] for v in jacket["variants"]} == {1000.0}


def test_the_scope_redirect_said_twice_is_not_a_repeat():
    """The prompt asks for the same redirect every time; the repeat rule used to
    refuse the second one and send a product-question fallback instead."""
    from assistant import reply_rules

    assert reply_rules.SCOPE_REDIRECT in SYSTEM_PROMPT
    redirect = "أنا هنا لخدمة عملاء رحلة بس. أقدر أساعد حضرتك في حاجة من اللي عندنا؟"
    found = reply_rules.violation(redirect, customer="إيه عاصمة فرنسا؟", previous=redirect, results=[])
    assert not found.startswith("repeat"), found


def test_the_summary_and_the_confirm_question_travel_together():
    assert "«أأكد الأوردر؟» في **نفس رسالة الملخص**" in SYSTEM_PROMPT


OFFER = "توب Rehla Original Tops لون Black مقاس M متوفر، السعر 445 جنيه. أضيفه للشنطة؟"


def test_a_yes_answered_with_the_same_offer_is_sent_back():
    """«اه» the next morning, and the reply asked «أضيفه للشنطة؟» again."""
    from assistant import reply_rules

    found = reply_rules.violation(OFFER, customer="اه", previous=OFFER, results=[])
    assert found.startswith("accepted"), found
    assert "accepted" in agent._RULE_NUDGE


def test_a_yes_that_was_acted_on_or_said_with_more_is_left_alone():
    from assistant import reply_rules

    added = [("add_to_cart", {"lines": []})]
    assert not reply_rules.offer_asked_again("اه", OFFER, "ضفته. محتاجة حاجة تانية؟", added)
    assert not reply_rules.offer_asked_again("اه بس مقاس L", OFFER, OFFER, [])
    assert not reply_rules.offer_asked_again("اه", "المقاسات المتاحة: S و M", OFFER, [])


def test_a_late_yes_answered_with_the_offer_again_is_retried_into_the_add(seeded):
    from datetime import timedelta

    from assistant.providers.base import ModelReply
    from assistant.providers.fake import ScriptedProvider
    from assistant.runtime import handle_message
    from domain.models import SessionRow, utcnow

    who = "201000000778"
    offer = "الهودي الزيتي مقاس S متوفر. أضيفه للشنطة؟"
    handle_message("whatsapp", who, "الهودي الزيتي S متاح؟", db=seeded,
                   provider=ScriptedProvider([ModelReply(text=offer)]))
    seeded.get(SessionRow, ("whatsapp", who)).updated_at = utcnow() - timedelta(hours=18)
    seeded.flush()
    provider = ScriptedProvider([
        ModelReply(text=offer),
        ModelReply(tool_calls=[{"id": "c1", "name": "add_to_cart",
                                "arguments": {"variant_id": "rehla-hoodie-s-olive"}}]),
        ModelReply(text="تمام، ضفته للشنطة."),
    ])
    reply = handle_message("whatsapp", who, "اه", db=seeded, provider=provider)
    assert "add_to_cart" in (reply.tool_calls or []), reply.text
