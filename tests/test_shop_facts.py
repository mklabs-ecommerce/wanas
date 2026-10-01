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
    assert "85 جنيه" not in prompt
    # ...including the layout example, which used to price Cairo at 60.
    assert "• الشحن للقاهرة — 125 جنيه" in prompt


def test_fees_that_differ_are_a_range_not_one_of_them(seeded):
    set_all_fees(seeded, 85)
    seeded.get(ShippingRate, "Cairo").fee = Decimal("70")
    seeded.flush()
    assert shop_facts.shipping_line(seeded) == "70 جنيه للقاهرة، و85 جنيه لباقي المحافظات"


def test_three_different_fees_are_a_range(seeded):
    set_all_fees(seeded, 85)
    seeded.get(ShippingRate, "Cairo").fee = Decimal("70")
    seeded.get(ShippingRate, "Aswan").fee = Decimal("100")
    seeded.flush()
    assert shop_facts.shipping_line(seeded) == "من 70 لـ 100 جنيه حسب المحافظة"


def test_without_a_table_the_shops_defaults_are_used():
    assert shop_facts.shipping_line() == "70 جنيه للقاهرة والجيزة، و85 جنيه لباقي المحافظات"
    assert f"«{shop_facts.shipping_line()}»" in SYSTEM_PROMPT
    assert "⟦" not in SYSTEM_PROMPT, "every slot is filled"


def test_the_exchange_terms_are_orders_own_numbers():
    assert EXCHANGE_WINDOW_HOURS == shop_facts.EXCHANGE_DAYS * 24
    assert f"خلال {shop_facts.EXCHANGE_DAYS} يوم من الاستلام" in SYSTEM_PROMPT
    assert f"خلال {shop_facts.RETURN_DAYS} أيام من الاستلام" in SYSTEM_PROMPT
    assert int(EXCHANGE_SURCHARGE) == 0, "Rehla charges nothing extra for an exchange"


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
