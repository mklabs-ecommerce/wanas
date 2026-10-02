"""Rehla's return & exchange policy, decided in code and stated in the prompt.

Exchange 14 days from delivery (size or defect, unused, tags and packaging);
return 7 days, shipping deducted; a manufacturing defect is on Rehla; used,
washed, untagged and discounted items are excluded unless defective; and a
request is the order number, then the team.
"""

from __future__ import annotations

from datetime import timedelta

from assistant.prompt import SYSTEM_PROMPT
from domain.models import Order, OrderStatus, utcnow
from domain.services import orders, shop_facts
from domain.services.shop_facts import return_eligibility


def test_ten_days_after_delivery_exchange_yes_return_no():
    terms = return_eligibility(10)
    assert terms["exchange"] is True
    assert terms["return"] is False


def test_inside_a_week_both_are_open():
    terms = return_eligibility(5)
    assert terms["exchange"] is True and terms["return"] is True
    assert terms["refund_deducts_shipping"] is True
    assert terms["shipping_paid_by"] == "customer"


def test_past_fourteen_days_nothing_is_open():
    terms = return_eligibility(15)
    assert terms["exchange"] is False and terms["return"] is False


def test_a_discounted_item_is_refused_unless_it_is_defective():
    plain = return_eligibility(3, discounted=True)
    assert plain["exchange"] is False and plain["return"] is False

    defective = return_eligibility(3, discounted=True, defect=True)
    assert defective["exchange"] is True and defective["return"] is True


def test_a_defect_puts_all_shipping_on_rehla():
    terms = return_eligibility(3, defect=True)
    assert terms["shipping_paid_by"] == "rehla"
    assert terms["refund_deducts_shipping"] is False


def test_used_washed_or_untagged_items_are_excluded():
    assert return_eligibility(1, used_or_washed=True)["exchange"] is False
    assert return_eligibility(1, tags_and_packaging=False)["return"] is False


def test_an_unrecorded_delivery_is_unknown_never_closed():
    terms = return_eligibility(None)
    assert terms["exchange"] == "unknown" and terms["return"] == "unknown"


def test_every_request_goes_to_the_team_with_the_order_number():
    assert return_eligibility(1)["next_step"] == "ask_order_number_then_request_human"
    assert orders.return_terms()["request_flow"] == "ask_order_number_then_request_human"


def test_return_terms_reads_both_windows_off_the_delivery_date():
    order = Order(
        order_id="o1",
        status=OrderStatus.DELIVERED.value,
        shipping_fee=70,
        delivered_at=utcnow() - timedelta(days=10),
    )
    terms = orders.return_terms(order)
    assert terms["exchange_window"] == "open"
    assert terms["return_window"] == "closed"


def test_the_prompt_states_the_policy():
    assert f"خلال {shop_facts.EXCHANGE_DAYS} يوم من الاستلام" in SYSTEM_PROMPT
    assert f"خلال {shop_facts.RETURN_DAYS} أيام من الاستلام" in SYSTEM_PROMPT
    assert "مصاريف الشحن بتتخصم" in SYSTEM_PROMPT
    assert "رحلة بتتحمل الشحن كله" in SYSTEM_PROMPT
    assert "القطع اللي عليها خصم (إلا لو فيها عيب صناعة)" in SYSTEM_PROMPT
    assert "اطلب رقم الأوردر الأول" in SYSTEM_PROMPT
