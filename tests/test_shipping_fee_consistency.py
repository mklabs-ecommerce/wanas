"""The shipping fee is published in three places and stored in a fourth.

The bot answers "how much is delivery" from a fixed sentence with no tool call,
and bills from `ShippingRate`. Nothing enforces that those agree, and the prompt
itself calls being surprised by a bigger number at the door the worst thing that
can happen on cash on delivery.
"""

from __future__ import annotations

import re
from decimal import Decimal

import app
from assistant.comment_faq import FAQ_REPLIES
from assistant.prompt import SYSTEM_PROMPT
from domain.models import ShippingRate


def _only_number(text: str) -> str:
    found = re.findall(r"[0-9]+", text)
    assert found, f"no number in {text!r}"
    return found[0]


def test_the_public_answer_and_the_prompt_quote_the_same_fee():
    """One is published under an Instagram post, the other is what the model
    repeats in a DM. A customer asking the same question in two places must
    not get two prices."""
    published = _only_number(FAQ_REPLIES["shipping_cost"])
    assert f"الشحن «{published} جنيه لكل محافظات مصر»" in SYSTEM_PROMPT


def test_the_boot_default_is_the_published_fee():
    """`_DEFAULT_SHIPPING_FEE` fills in any governorate nobody has priced, so
    if it disagreed with the published sentence the bot would quote one number
    and store another on the very first boot."""
    assert Decimal(_only_number(FAQ_REPLIES["shipping_cost"])) == app._DEFAULT_SHIPPING_FEE


def test_a_governorate_priced_differently_is_reported_at_boot(seeded, caplog):
    """The drift this exists to make visible: someone re-prices one
    governorate and the published sentence stays where it was."""
    rate = seeded.get(ShippingRate, "Cairo")
    rate.fee = app._DEFAULT_SHIPPING_FEE + Decimal("40")
    seeded.commit()

    with caplog.at_level("WARNING"):
        app._warn_if_shipping_rates_contradict_the_published_fee()

    assert any("priced differently" in r.getMessage() for r in caplog.records), caplog.records


def test_rates_that_all_match_say_nothing(seeded, caplog):
    for rate in seeded.query(ShippingRate).all():
        rate.fee = app._DEFAULT_SHIPPING_FEE
    seeded.commit()

    with caplog.at_level("WARNING"):
        app._warn_if_shipping_rates_contradict_the_published_fee()

    assert not [r for r in caplog.records if "shipping:" in (r.getMessage())]


def test_an_unpriced_governorate_is_reported(seeded, caplog):
    """An order for it cannot be confirmed -- `get_shipping_fee` returns
    `no_rate_set` -- so it is worth saying at boot rather than at checkout."""
    for rate in seeded.query(ShippingRate).all():
        rate.fee = app._DEFAULT_SHIPPING_FEE
    seeded.get(ShippingRate, "Cairo").fee = None
    seeded.commit()

    with caplog.at_level("WARNING"):
        app._warn_if_shipping_rates_contradict_the_published_fee()

    assert any("no fee set" in (r.getMessage()) for r in caplog.records)
