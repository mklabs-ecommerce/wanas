"""The last sentence before a handoff is the shop's, and a photo is matched
only against what the shop still sells.

`docs/LLM_AUDIT.md` findings 13 and 14:

* after `request_human` the conversation goes quiet until a person opens the
  dashboard, and the model's own sign-off was where a callback time nobody
  had promised («حد هيكلمك خلال ساعة») could be written. The turn now ends on
  a fixed sentence per reason, the way `confirm_order` ends on the shop's own
  confirmation;
* a customer's photo was read against every product, archived ones included,
  and the note that follows a match says «أقرب منتج عندنا هو ...».
"""

from __future__ import annotations

from assistant import agent, media, session as session_store
from assistant.providers.base import ImageReading, ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.tools.support_tools import HANDOFF_CLOSINGS
from domain.models import Product

CHANNEL = "whatsapp"
WHO = "201000000313"


def test_a_handoff_ends_on_the_shops_own_sentence(seeded):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {
                        "id": "h1",
                        "name": "request_human",
                        "arguments": {"reason": "complaint", "summary": "wrong size arrived"},
                    }
                ]
            ),
            ModelReply(text="حد من الفريق هيكلمك خلال ساعة بالظبط."),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "المقاس اللي وصلني غلط", provider=provider)

    assert reply.text == HANDOFF_CLOSINGS["complaint"]
    assert len(provider.calls) == 1, "the model is not asked for a sign-off"
    stored = session_store.transcript(seeded, CHANNEL, WHO)
    assert stored[-1]["role"] == "assistant"
    assert stored[-1]["content"] == HANDOFF_CLOSINGS["complaint"]


def test_every_reason_the_model_may_give_has_a_closing():
    from assistant.tools.support_tools import MODEL_HANDOFF_REASONS

    assert set(HANDOFF_CLOSINGS) == set(MODEL_HANDOFF_REASONS)


def test_an_archived_product_is_not_offered_to_the_photo_reader(seeded):
    seeded.get(Product, "cairokee-hoodie").archived = True
    seeded.flush()
    offered = {item["product_id"] for item in media.catalog_shortlist(seeded)}
    assert "cairokee-hoodie" not in offered
    assert "wanas-hoodie" in offered


def test_a_reading_that_lands_on_an_archived_product_is_no_match(seeded):
    seeded.get(Product, "cairokee-hoodie").archived = True
    seeded.flush()
    reading = ImageReading(product_id="cairokee-hoodie", confidence=0.95, description="", is_garment=True)
    assert media.matched_product(seeded, reading) is None
