"""A size chart is read fresh every time, never replayed from the conversation.

Production, 2026-09-25 13:18, the first minutes after the chart fix deployed:

    log: tool get_size_chart({'product_id': 'boxy-wns-tee'})
         served from session cache, not re-fetched
    log: ... -> ['has_chart', 'chart_id', 'title', 'unit']

The customer's conversation already held this morning's answer -- the Ringer
chart, looked up while the product row still pointed at it -- and the tool
cache replays any identical earlier call in the live conversation, for up to
six hours. So the corrected product answered with the old chart until the
conversation was reset by hand. The cache exists to save a Shopify round trip,
and a size chart is a local read with no round trip to save; replaying it only
ever risks a chart that has since been corrected -- at boot, as here, or by a
staff member in the dashboard mid-conversation.
"""

from __future__ import annotations

from assistant.tools.base import CACHEABLE_TOOLS, ToolContext, call_tool
from domain.models import Product

CHANNEL = "whatsapp"
WHO = "201067177129"


def _earlier_answer(chart_id: str, image: str) -> list[dict]:
    """This morning's lookup, as it sits in the stored conversation."""
    return [
        {"role": "user", "content": "ابعتلي جدول مقاسات Boxy WNS Tee"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "get_size_chart", "arguments": {"product_id": "boxy-wns-tee"}}
            ],
        },
        {
            "role": "tool_results",
            "results": [
                {
                    "id": "c1",
                    "name": "get_size_chart",
                    "content": {
                        "has_chart": True,
                        "chart_id": chart_id,
                        "title": "Ringer t-shirt",
                        "unit": "cm",
                        "measurements": [],
                        "sizes": {"S": {"width": 54, "length": 65}},
                        "image": image,
                    },
                }
            ],
        },
        {"role": "assistant", "content": "ده الجدول 👆", "attachments": [image]},
        {"role": "user", "content": "ابعته تاني"},
    ]


def test_a_chart_corrected_since_it_was_last_asked_for_is_the_corrected_one(seeded):
    ctx = ToolContext(
        session=seeded,
        channel=CHANNEL,
        external_id=WHO,
        history=_earlier_answer("ringer-boxy-tee", "data/size-charts/ringer-boxy-tee.png"),
    )
    assert seeded.get(Product, "boxy-wns-tee").size_chart == "wns-boxy-tee"

    payload = call_tool(ctx, "get_size_chart", {"product_id": "boxy-wns-tee"})

    assert payload["chart_id"] == "wns-boxy-tee"
    assert payload["sizes"]["S"] == {"width": 56, "length": 66}
    assert ctx.attachments == ["data/size-charts/wns-boxy-tee.png"]


def test_size_charts_are_not_replayed_from_the_conversation():
    assert "get_size_chart" not in CACHEABLE_TOOLS
