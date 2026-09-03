"""Regression coverage for the "out of scope" abandonment bug.

Two failures were live at once, and this file pins the fix for both.

1. `request_human`'s own schema listed `out_of_scope` as a reason the model
   could pick, right next to `unclear`, `complaint` and `customer_asked`. The
   prompt has always said an off-topic question is answered with one line and
   never escalated -- but that rule lived 200 lines away from the tool the
   model actually calls, and a schema enum reads as a menu. A model that
   decided a message "did not fit" had a tool that would happily pause the
   conversation over it. `MODEL_HANDOFF_REASONS` in
   `assistant/tools/support_tools.py` closes the gap: the tool itself now
   refuses `out_of_scope`, and refuses a first `unclear` before one
   clarifying question has been asked.

2. Even with that closed, a conversation can still end up paused for a bad
   reason -- staff judgement calls this "false positive handoffs" and it will
   never hit exactly zero. `assistant/recovery.py` is the net under that: a
   customer who writes back inside ten minutes of a *message-shaped* handoff
   (`unclear`, `out_of_scope` -- never `complaint` or `customer_asked`, which
   are a person's decision) gets the conversation back automatically, with
   the recent transcript read as one piece and the reply aimed at their
   newest message.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from assistant import agent, recovery, runtime, session as session_store
from assistant.providers.base import ModelReply
from assistant.providers.fake import ScriptedProvider
from assistant.tools.base import ToolContext, call_tool
from assistant.tools.support_tools import HANDOFF_REASONS, raise_handoff
from common.timeutil import utcnow
from domain.models import QueueKind, QueueStatus
from domain.services import identities, queues

CHANNEL = "whatsapp"
WHO = "201000000001"


def call(ctx, name, **arguments):
    return call_tool(ctx, name, arguments)


@pytest.fixture()
def ctx(seeded):
    return ToolContext(session=seeded, channel=CHANNEL, external_id=WHO, history=[])


# --------------------------------------------------------------------------
# 1. The tool refuses what the prompt already forbade
# --------------------------------------------------------------------------


def test_request_human_refuses_out_of_scope():
    """The exact bug: the model could call request_human(reason='out_of_scope')
    for an off-topic question, and nothing behind the schema would stop it."""
    from assistant.tools.support_tools import MODEL_HANDOFF_REASONS

    assert "out_of_scope" not in MODEL_HANDOFF_REASONS


def test_request_human_refuses_out_of_scope_at_the_tool(ctx, seeded):
    result = call(ctx, "request_human", reason="out_of_scope", summary="asked about the weather")
    assert result["error"] == "scope_is_not_a_handoff"
    # And, critically, nothing was actually queued or paused.
    assert identities.is_paused(seeded, CHANNEL, WHO) is False
    assert queues.open_items(seeded, QueueKind.HANDOFF.value) == []


def test_request_human_still_accepts_a_reason_it_always_had(ctx, seeded):
    result = call(ctx, "request_human", reason="complaint", summary="wrong item arrived")
    assert result == {"queued": True, "conversation_paused": True}
    assert identities.is_paused(seeded, CHANNEL, WHO) is True


def test_request_human_refuses_a_first_unclear_and_asks_for_a_clarifying_attempt(ctx, seeded):
    """Step 2 of the prompt's own handoff section says ask one clarifying
    question before escalating an unclear message. This is the tool holding
    it to that -- refusing the first attempt, not every attempt."""
    result = call(ctx, "request_human", reason="unclear", summary="no idea what they mean")
    assert result["error"] == "clarify_first"
    assert identities.is_paused(seeded, CHANNEL, WHO) is False


def test_request_human_allows_unclear_after_a_clarifying_question_was_asked(seeded):
    """The second attempt, once the model has actually asked something and the
    customer has answered, must go through -- this is not a permanent block,
    only a first-attempt one."""
    history = [
        {"role": "user", "content": "حاجة"},
        {
            "role": "tool_results",
            "results": [
                {
                    "id": "1",
                    "name": "request_human",
                    "content": {"error": "clarify_first", "detail": "..."},
                }
            ],
        },
        {"role": "assistant", "content": "تقصد منتج معين؟"},
        {"role": "user", "content": "معرفش"},
    ]
    ctx2 = ToolContext(session=seeded, channel=CHANNEL, external_id=WHO, history=history)
    result = call(ctx2, "request_human", reason="unclear", summary="still no idea")
    assert result == {"queued": True, "conversation_paused": True}


def test_out_of_scope_remains_a_valid_reason_for_the_column_itself():
    """The channel adapters (an unsupported message type) and the runtime
    (image/voice the provider could not read) still raise these reasons
    directly, bypassing the tool -- so the wider vocabulary must still exist."""
    assert "out_of_scope" in HANDOFF_REASONS
    assert "image_received" in HANDOFF_REASONS
    assert "voice_received" in HANDOFF_REASONS


def test_the_agent_never_leaves_an_off_topic_conversation(seeded):
    """End-to-end proof of the fix: a model that tries the old escalation
    gets refused and, on the next turn, answers normally instead."""
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {
                        "id": "a",
                        "name": "request_human",
                        "arguments": {"reason": "out_of_scope", "summary": "asked for the capital of France"},
                    }
                ]
            ),
            ModelReply(text="أنا هنا لونس بس 😅 تحب أوريك حاجة من اللي عندنا؟"),
        ]
    )
    reply = agent.run_turn(seeded, CHANNEL, WHO, "ايه عاصمة فرنسا؟", provider=provider)
    assert identities.is_paused(seeded, CHANNEL, WHO) is False
    assert queues.open_items(seeded, QueueKind.HANDOFF.value) == []
    assert reply.text == "أنا هنا لونس بس 😅 تحب أوريك حاجة من اللي عندنا؟"


# --------------------------------------------------------------------------
# 2. Recovery: taking a wrongly-abandoned conversation back
# --------------------------------------------------------------------------


def _raise(seeded, reason: str, summary: str = "x", **payload):
    item = raise_handoff(seeded, CHANNEL, WHO, reason, summary, payload=payload or None)
    seeded.commit()
    return item


def test_a_resumable_handoff_is_found_inside_the_window(seeded):
    _raise(seeded, "unclear")
    item = recovery.resumable_handoff(seeded, CHANNEL, WHO, [])
    assert item is not None
    assert item.reason == "unclear"


def test_complaint_and_customer_asked_are_never_auto_resumed(seeded):
    """A person's own decision to ask for staff, or to report something wrong,
    must never be second-guessed by the bot itself."""
    _raise(seeded, "complaint")
    assert recovery.resumable_handoff(seeded, CHANNEL, WHO, []) is None


def test_customer_asked_is_never_auto_resumed(seeded):
    _raise(seeded, "customer_asked")
    assert recovery.resumable_handoff(seeded, CHANNEL, WHO, []) is None


def test_image_received_is_never_auto_resumed(seeded):
    """The photo is still unread; that is what staff were queued to look at,
    and a text reply afterwards does not change that."""
    _raise(seeded, "image_received")
    assert recovery.resumable_handoff(seeded, CHANNEL, WHO, []) is None


def test_a_handoff_outside_the_resume_window_is_not_taken_back(seeded):
    item = _raise(seeded, "unclear")
    item.created_at = utcnow() - recovery.RESUME_WINDOW - timedelta(minutes=1)
    seeded.commit()
    assert recovery.resumable_handoff(seeded, CHANNEL, WHO, []) is None


def test_a_handoff_just_inside_the_window_is_taken_back(seeded):
    item = _raise(seeded, "unclear")
    item.created_at = utcnow() - recovery.RESUME_WINDOW + timedelta(seconds=30)
    seeded.commit()
    assert recovery.resumable_handoff(seeded, CHANNEL, WHO, []) is not None


def test_a_staff_takeover_stops_the_auto_resume(seeded):
    """Once a person has manually pulled a conversation under control (even
    without replying yet), the bot's own next message must not pull it back."""
    from dashboard import web as dashboard_web

    item = _raise(seeded, "unclear")
    assert item.payload.get("auto_resume_after_abandonment") is True

    # What dashboard.web.takeover does to the payload, exercised directly
    # against the same row rather than through the HTTP route/session-scope
    # machinery this test file does not otherwise set up.
    payload = dict(item.payload or {})
    payload.pop("auto_resume_after_abandonment", None)
    item.payload = payload
    seeded.commit()

    assert recovery.resumable_handoff(seeded, CHANNEL, WHO, []) is None
    assert dashboard_web is not None  # imported to prove the module still wires cleanly


def test_a_resolved_handoff_is_not_resumed(seeded):
    """The dashboard's own reply route resolves the item as it sends -- a
    resolved handoff means staff already answered, so the bot must not also."""
    item = _raise(seeded, "unclear")
    item.status = QueueStatus.RESOLVED.value
    seeded.commit()
    assert recovery.resumable_handoff(seeded, CHANNEL, WHO, []) is None


def test_a_staff_message_after_the_handoff_stops_the_resume(seeded):
    item = _raise(seeded, "unclear")
    raised_at = item.created_at
    history = [
        {"role": "assistant", "content": "حد هيرد عليك", "by": "staff", "at": (raised_at + timedelta(seconds=5)).isoformat()},
    ]
    assert recovery.resumable_handoff(seeded, CHANNEL, WHO, history) is None


def test_take_back_leaves_the_queue_item_open_but_stamps_it(seeded):
    """Auto-resuming must not read as staff having handled it -- a false
    positive is still worth a person's eye, so the item stays open."""
    item = _raise(seeded, "unclear")
    recovery.take_back(seeded, item)
    seeded.commit()
    assert item.status == QueueStatus.OPEN.value
    assert "auto_resume_after_abandonment" not in (item.payload or {})
    assert item.payload.get("auto_resumed_at")


# --------------------------------------------------------------------------
# 3. Runtime end-to-end: the bot resumes on its own next message, with the
#    recent transcript as context and the reply aimed at the newest message
# --------------------------------------------------------------------------


def test_runtime_resumes_and_answers_only_the_latest_message(seeded):
    """The concrete scenario the fix is for: the bot abandoned a conversation
    (an out_of_scope handoff, exactly as the unsupported-message-type path
    raises), the customer wrote back a few minutes later, and the reply must
    (a) be built from the recent context and (b) answer the newest message
    specifically -- not restate an old one, not open with an apology."""
    # Build up a short shopping conversation, then the abandonment, entirely
    # through the stored session -- this is what the model would have seen
    # if the earlier turns had actually gone through the agent, and it is
    # exactly what `session_store.load` hands back to `recovery`.
    for role, text in [
        ("user", "عايز WANAS Hoodie أسود مقاس L"),
        ("assistant", "تمام، حطيتلك الأسود L. تحب حاجة تانية؟"),
        ("user", "لأ بس عايز أعرف ليه بتلبسوا ألوان زيتي في الصور؟"),
    ]:
        session_store.append(seeded, CHANNEL, WHO, {"role": role, "content": text})

    # This is the abandonment: raised the same way the channel adapter raises
    # it for a message type the bot cannot open, or the way the old prompt
    # gap let the model raise it for an off-topic question.
    _raise(seeded, "out_of_scope", "asked something unrelated mid-flow")
    assert identities.is_paused(seeded, CHANNEL, WHO) is True

    # The customer writes back well inside the ten-minute window.
    provider = ScriptedProvider(
        [ModelReply(text="اه فاهم قصدك دلوقتي، تحب تأكد الأوردر بتاع الهودي الأسود L؟")]
    )
    reply = runtime.handle_message(
        CHANNEL, WHO, "طب خلاص، أكد الأوردر", db=seeded, provider=provider
    )

    # The conversation carried on: no fresh handoff, no pause, an actual reply.
    assert reply.paused is False
    assert reply.text == "اه فاهم قصدك دلوقتي، تحب تأكد الأوردر بتاع الهودي الأسود L؟"
    assert identities.is_paused(seeded, CHANNEL, WHO) is False

    # The model saw the resume instruction for exactly this one turn.
    seen_system_prompt = provider.calls[0][0]
    assert "المحادثة دي رجعت لك" in seen_system_prompt
    # And it saw the earlier turns -- the hoodie, the off-topic question --
    # not just the isolated new message, which is the batched-context half of
    # the fix.
    seen_history = provider.calls[0][1]
    seen_text = "\n".join(m.get("content", "") for m in seen_history if m.get("role") == "user")
    assert "WANAS Hoodie" in seen_text
    assert "طب خلاص، أكد الأوردر" in seen_text

    # The original handoff is left open for a person to still see, but marked
    # as auto-resumed rather than silently vanished.
    seeded.expire_all()
    item = queues.open_items(seeded, QueueKind.HANDOFF.value)[0]
    assert item.reason == "out_of_scope"
    assert item.payload.get("auto_resumed_at")


def test_runtime_does_not_resume_a_complaint_handoff(seeded):
    """The other half of the same end-to-end path: a handoff that is a
    person's judgement call must still behave exactly as before -- silent,
    paused, waiting on staff."""
    _raise(seeded, "complaint", "damaged item")
    provider = ScriptedProvider([ModelReply(text="should never be reached")])
    reply = runtime.handle_message(CHANNEL, WHO, "في حد؟", db=seeded, provider=provider)
    assert reply.paused is True
    assert identities.is_paused(seeded, CHANNEL, WHO) is True
    assert provider.calls == []


def test_runtime_does_not_resume_outside_the_window(seeded):
    item = _raise(seeded, "unclear")
    item.created_at = utcnow() - recovery.RESUME_WINDOW - timedelta(minutes=5)
    seeded.commit()
    provider = ScriptedProvider([ModelReply(text="should never be reached")])
    reply = runtime.handle_message(CHANNEL, WHO, "لسه فاضل؟", db=seeded, provider=provider)
    assert reply.paused is True
    assert provider.calls == []
