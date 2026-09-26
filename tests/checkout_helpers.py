"""What every real checkout does before `confirm_order` may run.

`order_tools._not_yet_agreed` refuses an order the customer has not said yes
to: the bot's last message has to state the total `get_shipping_fee`'s
`checkout` gives, and the customer's answer has to agree. Tests that place an
order by calling the tool directly walk the same two steps here.
"""

from __future__ import annotations

from assistant import messages as msg
from assistant.tools.base import ToolContext
from assistant.tools.catalog_tools import _checkout
from domain.services import shipping


def agree_to_the_summary(ctx: ToolContext, governorate: str) -> None:
    """Append the summary with the real total, and the customer's yes."""
    resolved = shipping.resolve(ctx.session, governorate)
    fee = shipping.get_fee(ctx.session, resolved) if resolved else None
    checkout = _checkout(ctx, fee) if fee is not None else None
    total = checkout["total"] if checkout else ""
    ctx.history.append(msg.assistant(f"الإجمالي {total} جنيه كاش عند الاستلام. أأكد الأوردر؟"))
    ctx.history.append(msg.user("اه أكد"))
