"""Prompt surface awareness (STEP 13).

The WhatsApp system prompt is pinned by conversation-behaviour tests and
tuned against real conversations -- it must come out byte-identical to what
it always was. These tests pin that guarantee and pin what the Instagram
variant adds instead.
"""

from __future__ import annotations

from assistant.prompt import (
    INSTAGRAM_SURFACE_LINE,
    SYSTEM_PROMPT,
    build_system_prompt,
)


def test_the_default_prompt_is_unchanged_byte_for_byte():
    """No channel argument means WhatsApp, which means the historical string,
    untouched."""
    assert build_system_prompt() == SYSTEM_PROMPT
    assert build_system_prompt(channel="whatsapp") == SYSTEM_PROMPT


def test_the_whatsapp_surface_line_is_still_in_the_shared_prompt():
    assert "بتتكلم مع الزباين على واتساب" in SYSTEM_PROMPT


def test_the_instagram_prompt_swaps_the_surface_line():
    prompt = build_system_prompt(channel="instagram_dm")

    assert INSTAGRAM_SURFACE_LINE in prompt
    # The WhatsApp wording is gone from it.
    assert "على واتساب" not in prompt


def test_the_instagram_prompt_carries_only_the_instagram_paragraph():
    prompt = build_system_prompt(channel="instagram_dm")

    assert "لو بتتكلم على الانستجرام" in prompt
    assert "حد للطول" in prompt                      # shorter replies / byte cap
    assert "مفيش قوايم بتتداس" in prompt             # no tappable lists: prose
    assert "كومنت على بوست" in prompt                # comment-origin context
    # And no Instagram paragraph leaks into the shared string itself.
    assert "لو بتتكلم على الانستجرام" not in SYSTEM_PROMPT


def test_the_extra_argument_still_applies_per_channel():
    extra = "# قاعدة مؤقتة"
    whatsapp = build_system_prompt(extra)
    instagram = build_system_prompt(extra, channel="instagram_dm")

    assert whatsapp.endswith(f"\n\n{extra}")
    assert instagram.endswith(f"\n\n{extra}")


def test_run_turn_builds_the_prompt_for_its_own_channel(seeded):
    """The agent threads `channel` through, so an Instagram turn never reads
    a WhatsApp-shaped prompt."""
    captured = {}
    import assistant.agent as agent_module

    class RecordingProvider:
        name = "recording"
        supports_audio = False
        supports_vision = False

        def generate(self, system_prompt, history, specs):
            captured["system_prompt"] = system_prompt
            from assistant.providers.base import ProviderReply

            return ProviderReply(text="تمام")

    provider = RecordingProvider()
    agent_module.run_turn(
        seeded, "instagram_dm", "98765432109876543", "بكام؟", provider=provider
    )

    assert INSTAGRAM_SURFACE_LINE in captured["system_prompt"]

    agent_module.run_turn(
        seeded, "whatsapp", "201555999111", "بكام؟", provider=provider
    )
    assert captured["system_prompt"] == SYSTEM_PROMPT