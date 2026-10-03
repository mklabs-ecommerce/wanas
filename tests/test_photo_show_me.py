"""«وريهوني» answered with a photograph -- every time it is asked.

Production, 2026-10: the customer sent a photo of a pink tee, was offered the
closest top, and then asked three times to see it -- «وريهوني كدا», «عايز
الصورة بتاعته», «عايز اشوفه الاول». The bot called `get_variants` each time
and attached nothing: the photo counted as already shown, and the plain
"show me the product" branch returns empty-handed for a product already seen.
The model then explained the empty reply as «معلش، الصورة ماتبعتتش المرة دي»
-- a failure that never happened.
"""

from __future__ import annotations

from assistant import agent, photo_claims, session as session_store
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.showcase import _SEE_VERB

CHANNEL = "whatsapp"
WHO = "201000000077"
PRODUCT = "ringer-tee"


def _turn(seeded, text, *replies):
    return agent.run_turn(seeded, CHANNEL, WHO, text, provider=ScriptedProvider(list(replies)))


def _variants(color=None):
    arguments = {"product_id": PRODUCT, **({"color": color} if color else {})}
    return ModelReply(tool_calls=[{"id": "v1", "name": "get_variants", "arguments": arguments}])


def test_show_me_resends_a_photo_already_shown(seeded):
    first = _turn(seeded, "عندكم Ringer Tee؟", _variants(), ModelReply(text="أيوه، تيشيرت Ringer Tee متوفر."))
    assert first.attachments, "the first showing carried no photo"
    seeded.commit()

    for ask in ("وريهوني كدا", "عايز الصورة بتاعته", "عايز اشوفه الاول"):
        reply = _turn(seeded, ask, _variants(), ModelReply(text="دي صورته 🙂"))
        photos = [p for p in reply.attachments if not p.startswith("data/size-charts/")]
        assert photos, f"«{ask}» was answered with no photograph"
        seeded.commit()


def test_see_verbs_cover_the_egyptian_spellings():
    for text in ("وريهوني كدا", "ورّيني", "وريني الالوان", "عايز اشوفه"):
        assert _SEE_VERB.search(text), text


def test_a_failed_send_with_nothing_refused_is_caught():
    assert photo_claims.claims_failed_send("معلش، الصورة ماتبعتتش المرة دي", [])
    # A real refusal on record makes the same sentence the truth.
    refused = [{"role": "assistant", "content": "x", "undelivered_attachments": {"a.jpg": "Tee"}}]
    assert photo_claims.claims_failed_send("معلش، الصورة ماتبعتتش", refused) == ""
    # Not about a photograph at all.
    assert photo_claims.claims_failed_send("الأوردر ماتبعتش لسه", []) == ""


def test_the_false_failure_reply_never_reaches_the_customer(seeded):
    """The model keeps saying the photo failed; the reply that leaves does not."""
    _turn(seeded, "عندكم Ringer Tee؟", _variants(), ModelReply(text="أيوه، تيشيرت Ringer Tee متوفر."))
    seeded.commit()
    lie = ModelReply(text="معلش، الصورة ماتبعتتش المرة دي.")
    reply = _turn(seeded, "وريهوني كدا", _variants(), lie, lie, lie, lie)
    assert "ماتبعتتش" not in (reply.text or "")
    stored = session_store.load(seeded, CHANNEL, WHO)
    assert "ماتبعتتش" not in str(stored[-1].get("content"))
