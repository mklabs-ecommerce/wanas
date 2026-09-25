"""The facts the shop publishes are read from where they are kept.

Shipping, delivery time, payment, the exchange window and surcharge used to be
literals in the prompt and the public comment answers -- second copies of
numbers whose source of truth is the rate table and `orders.py`. A fee
changed in the dashboard changed every order and left the bot quoting the old
one. `domain/services/shop_facts.py` renders each sentence from its source.
"""

from __future__ import annotations

from decimal import Decimal

from assistant import agent, comment_faq
from assistant.prompt import SYSTEM_PROMPT, build_system_prompt
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from domain.models import ShippingRate
from domain.services import shop_facts
from domain.services.orders import EXCHANGE_SURCHARGE, EXCHANGE_WINDOW_HOURS


def set_all_fees(session, fee) -> None:
    for rate in session.query(ShippingRate).all():
        rate.fee = Decimal(str(fee))
    session.flush()


def test_the_prompt_quotes_the_fee_the_rate_table_holds(seeded):
    set_all_fees(seeded, 125)
    prompt = build_system_prompt(session=seeded)
    assert "«125 جنيه لكل محافظات مصر»" in prompt
    assert "110 جنيه" not in prompt
    # ...including the layout example, which used to price Cairo at 60.
    assert "• الشحن للقاهرة — 125 جنيه" in prompt


def test_fees_that_differ_are_a_range_not_one_of_them(seeded):
    set_all_fees(seeded, 110)
    seeded.get(ShippingRate, "Cairo").fee = Decimal("80")
    seeded.flush()
    assert shop_facts.shipping_line(seeded) == "من 80 لـ 110 جنيه حسب المحافظة"


def test_without_a_table_the_shops_defaults_are_used():
    assert shop_facts.shipping_line() == "110 جنيه لكل محافظات مصر"
    assert "«110 جنيه لكل محافظات مصر»" in SYSTEM_PROMPT
    assert "⟦" not in SYSTEM_PROMPT, "every slot is filled"


def test_the_exchange_terms_are_orders_own_numbers():
    assert f"خلال {EXCHANGE_WINDOW_HOURS} ساعة" in SYSTEM_PROMPT
    assert f"وزيادة {int(EXCHANGE_SURCHARGE)} جنيه" in SYSTEM_PROMPT


def test_the_public_answer_and_the_dm_say_the_same_fee(seeded):
    set_all_fees(seeded, 125)
    assert comment_faq.reply_for("shipping_cost", seeded) == "الشحن 125 جنيه لكل محافظات مصر."
    assert comment_faq.reply_for("payment") == shop_facts.PAYMENT_LINE
    assert f"«{shop_facts.PAYMENT_LINE.rstrip('.')}»" in SYSTEM_PROMPT


def test_a_live_turn_is_built_from_the_table(seeded):
    set_all_fees(seeded, 125)
    seeded.commit()
    provider = ScriptedProvider([ModelReply(text="أهلاً! تحب أساعدك في إيه؟")])
    agent.run_turn(seeded, "whatsapp", "201000000321", "السلام عليكم", provider=provider)
    assert "«125 جنيه لكل محافظات مصر»" in provider.calls[0][0]
