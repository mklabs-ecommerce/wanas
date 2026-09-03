"""The OpenRouter provider.

No network anywhere: httpx is stubbed exactly as in test_gemini_provider.py,
because the point is the translation between the neutral message format and
the OpenAI-compatible wire shapes, the error kinds, and the decision of when
voice/photo support is declared -- not OpenRouter's uptime or its models'.

The integration test in section 5 drives the real agent loop through this
provider, which is what proves the tool-call translation actually matches
what agent.py consumes. Section 6 covers voice and photos -- both ride the
same chat/completions endpoint and the same key as chat (an input_audio /
image_url content part respectively), but on their own model id: the
conversation model has no audio endpoint, so DEFAULT_MEDIA_MODEL is a
separate default rather than a fallback to chat. There is no Gemini anywhere in this provider; that stays
exercised through LLM_PROVIDER=gemini in test_gemini_provider.py.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging

import httpx
import pytest

from assistant import agent, messages as msg
from assistant.providers import build_provider, openrouter as openrouter_module
from assistant.providers.base import ProviderError
from assistant.providers.openrouter import (
    DEFAULT_MEDIA_MODEL,
    DEFAULT_MODEL,
    OpenRouterProvider,
)
from assistant.tools.base import tool_specs
from config.settings import load_settings, settings

CHANNEL = "whatsapp"
WHO = "201000000002"

KEY = "sk-or-not-a-real-key"

# An opaque blob from a Gemini-era session: it must travel into this provider's
# history translation and be dropped, never sent and never inspected.
SIG = "CvQBAdHtim9sig-from-a-gemini-session"


class FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


def text_reply(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def tool_call_reply(*calls: tuple[str, str, dict], text: str | None = None) -> dict:
    message: dict = {"role": "assistant", "content": text}
    message["tool_calls"] = [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for call_id, name, args in calls
    ]
    return {"choices": [{"message": message, "finish_reason": "tool_calls"}]}


@pytest.fixture()
def captured(monkeypatch):
    """Capture every request the provider sends; queue replies in order."""
    sent: list[dict] = []
    queue: list[FakeResponse | dict] = []

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.append({"url": url, "body": json, "headers": headers or {}, "timeout": timeout})
        if not queue:
            return FakeResponse(text_reply("ok"))
        nxt = queue.pop(0)
        return nxt if isinstance(nxt, FakeResponse) else FakeResponse(nxt)

    monkeypatch.setattr(openrouter_module.httpx, "post", fake_post)
    return {"sent": sent, "queue": queue}


@pytest.fixture()
def provider():
    return OpenRouterProvider(api_key=KEY)


# --------------------------------------------------------------------------
# 1. Construction, key handling, dispatch
# --------------------------------------------------------------------------


def test_a_missing_key_is_an_auth_problem():
    with pytest.raises(ProviderError) as excinfo:
        OpenRouterProvider(api_key="")
    assert excinfo.value.kind == "auth"


def test_the_key_comes_from_settings_when_not_passed(monkeypatch):
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(settings, openrouter_api_key="sk-or-from-settings"),
    )
    assert OpenRouterProvider().api_key == "sk-or-from-settings"


def test_the_openrouter_env_var_reaches_settings(monkeypatch):
    """The new settings field reads OPENROUTER_API_KEY via the usual loader."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env-key")
    assert load_settings().openrouter_api_key == "sk-or-env-key"


def test_the_default_model_is_pinned_and_an_explicit_one_wins():
    assert OpenRouterProvider(api_key=KEY).model == DEFAULT_MODEL
    assert OpenRouterProvider(api_key=KEY, model="openai/other").model == "openai/other"


def test_a_chat_call_waits_six_minutes_by_default(captured):
    """Thirty seconds turned the conversation model's tail into failed turns.

    `z-ai/glm-5.3-flash` has a median around six seconds but was measured at
    115s and 125s on two of eight single-hop calls against this prompt and
    these tools. Under the old thirty-second ceiling those two raised
    ReadTimeout, which reaches the customer as the generic apology -- a turn
    that answers slowly is strictly better than one that does not answer.

    Affordable only because nobody waits on it: the webhook returns 200 and
    the turn runs afterwards on a dispatcher worker (see
    test_the_dispatcher_returns_before_the_work_is_done in test_dispatcher.py).
    """
    provider = OpenRouterProvider(api_key=KEY)
    assert provider.timeout == 360.0
    assert OpenRouterProvider.DEFAULT_TIMEOUT == 360.0

    provider.generate("p", [msg.user("hi")], [])
    assert captured["sent"][0]["timeout"] == 360.0


def test_an_explicit_timeout_still_wins_and_does_not_shorten_media(captured):
    """A caller pinning a short chat timeout must not silently shorten a
    transcription too: media keeps its 60s floor."""
    provider = OpenRouterProvider(api_key=KEY, timeout=5.0)
    assert provider.timeout == 5.0
    assert provider.media_timeout == 60.0

    provider.generate("p", [msg.user("hi")], [])
    assert captured["sent"][0]["timeout"] == 5.0


def test_openrouter_is_dispatched_by_build_provider(monkeypatch):
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(settings, openrouter_api_key="sk-or-dispatch"),
    )
    provider = build_provider("openrouter")
    assert isinstance(provider, OpenRouterProvider)
    assert provider.name == "openrouter"
    assert provider.api_key == "sk-or-dispatch"


def test_an_unknown_provider_name_still_refuses():
    with pytest.raises(ProviderError):
        build_provider("not-a-provider")


# --------------------------------------------------------------------------
# 2. Request shape
# --------------------------------------------------------------------------


def test_the_request_shape(captured, provider):
    provider.generate("system prompt", [msg.user("hi")], [])
    sent = captured["sent"][0]
    assert sent["url"] == f"{openrouter_module.BASE_URL}/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    body = sent["body"]
    assert body["model"] == DEFAULT_MODEL
    assert body["messages"][0] == {"role": "system", "content": "system prompt"}
    assert {"role": "user", "content": "hi"} in body["messages"]
    assert body["temperature"] == 0.3
    # No tools asked for, no tools declared.
    assert "tools" not in body


def test_the_api_key_is_never_in_the_body(captured, provider):
    provider.generate("prompt", [msg.user("عايز هودي")], tool_specs()[:2])
    assert KEY not in json.dumps(captured["sent"][0]["body"], ensure_ascii=False)


def test_tool_schemas_are_translated_to_openai_function_shapes(captured, provider):
    specs = tool_specs()
    with_args = next(s for s in specs if s.properties)
    without_args = next(s for s in specs if not s.properties)
    provider.generate("p", [msg.user("x")], [with_args, without_args])
    assert captured["sent"][0]["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": with_args.name,
                "description": with_args.description,
                "parameters": {
                    "type": "object",
                    "properties": with_args.properties,
                    "required": list(with_args.required),
                },
            },
        },
        # A function with no arguments omits `parameters` entirely -- the same
        # lesson the Gemini side learned.
        {
            "type": "function",
            "function": {"name": without_args.name, "description": without_args.description},
        },
    ]


def test_assistant_tool_calls_go_out_as_openai_calls_and_results_as_tool_messages(captured, provider):
    history = [
        msg.user("عايز الهودي"),
        msg.assistant("", [msg.tool_call("call_7", "get_variants", {"product_id": "wanas-hoodie"}, SIG)], signature=SIG),
        msg.tool_results([msg.tool_result("call_7", "get_variants", {"variants": []})]),
    ]
    provider.generate("p", history, [])

    messages = captured["sent"][0]["body"]["messages"]
    assert messages[1] == {"role": "user", "content": "عايز الهودي"}
    assistant = messages[2]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"] == [
        {
            "id": "call_7",
            "type": "function",
            # Arguments travel out as a JSON string, per the wire format.
            "function": {"name": "get_variants", "arguments": '{"product_id": "wanas-hoodie"}'},
        }
    ]
    assert messages[3] == {"role": "tool", "tool_call_id": "call_7", "content": '{"variants": []}'}
    # A signature from another provider's session is dropped, never replayed.
    assert "sig-" not in json.dumps(messages)


def test_plain_text_history_passes_through(captured, provider):
    history = [msg.user("هاي"), msg.assistant("أهلاً بيك")]
    provider.generate("p", history, [])
    messages = captured["sent"][0]["body"]["messages"]
    assert messages[1] == {"role": "user", "content": "هاي"}
    assert messages[2] == {"role": "assistant", "content": "أهلاً بيك"}


def test_payload_logging_shows_the_body_and_never_the_key(captured, provider, monkeypatch, caplog):
    monkeypatch.setattr(
        openrouter_module, "settings", dataclasses.replace(settings, llm_debug_payload=True)
    )
    with caplog.at_level(logging.WARNING):
        provider.generate("sys prompt here", [msg.user("عايز هودي")], tool_specs()[:1])

    assert "payload" in caplog.text
    assert "عايز هودي" in caplog.text  # readable, not \u-escaped
    assert KEY not in caplog.text


# --------------------------------------------------------------------------
# 3. Response parsing -- the shape agent.py consumes
# --------------------------------------------------------------------------


def test_a_text_reply_parses(captured, provider):
    captured["queue"].append(text_reply("أهلاً"))
    reply = provider.generate("p", [msg.user("هاي")], [])
    assert reply.text == "أهلاً"
    assert reply.tool_calls == []
    assert reply.signature is None
    assert reply.finish_reason == "stop"


def test_tool_calls_are_translated_back_to_dicts(captured, provider):
    captured["queue"].append(
        tool_call_reply(("call_1", "get_products", {"query": "hoodie"}), ("call_2", "view_cart", {}))
    )
    reply = provider.generate("p", [msg.user("شنو عندك")], [])
    assert reply.finish_reason == "tool_calls"
    assert reply.tool_calls == [
        {"id": "call_1", "name": "get_products", "arguments": {"query": "hoodie"}},
        {"id": "call_2", "name": "view_cart", "arguments": {}},
    ]


def test_null_content_with_tool_calls_yields_empty_text(captured, provider):
    captured["queue"].append(tool_call_reply(("call_1", "view_cart", {}), text=None))
    reply = provider.generate("p", [msg.user("cart")], [])
    assert reply.text == ""
    assert len(reply.tool_calls) == 1


def test_empty_choices_is_an_empty_reply_not_a_crash(captured, provider):
    captured["queue"].append(FakeResponse({}))
    reply = provider.generate("p", [msg.user("hi")], [])
    assert reply.text == ""
    assert reply.tool_calls == []
    assert reply.finish_reason == "no_candidates"


def test_unparseable_arguments_are_rejected_not_dressed_up(captured, provider):
    """Garbage arguments must not reach the tools looking like empty ones --
    that would read as refusals the model never made."""
    captured["queue"].append(
        FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_x",
                                    "type": "function",
                                    "function": {"name": "get_products", "arguments": "{not json"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.generate("p", [msg.user("hi")], [])
    assert "unparseable" in str(excinfo.value)
    assert "call_x" in str(excinfo.value)


# --------------------------------------------------------------------------
# 4. Error mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_names_its_kind(captured, provider, status):
    captured["queue"].append(FakeResponse({}, status_code=status, text="invalid key"))
    with pytest.raises(ProviderError) as excinfo:
        provider.generate("p", [msg.user("hi")], [])
    assert excinfo.value.kind == "auth"
    assert KEY[-4:] in str(excinfo.value)  # masked, never the whole key
    assert KEY not in str(excinfo.value)


def test_rate_limiting_names_its_kind(captured, provider):
    captured["queue"].append(FakeResponse({}, status_code=429, text='{"error": "quota"}'))
    with pytest.raises(ProviderError) as excinfo:
        provider.generate("p", [msg.user("hi")], [])
    assert excinfo.value.kind == "rate_limit"
    assert DEFAULT_MODEL in str(excinfo.value)


def test_other_errors_keep_the_response_body(captured, provider):
    captured["queue"].append(FakeResponse({}, status_code=400, text='{"error":{"message":"bad request"}}'))
    with pytest.raises(ProviderError) as excinfo:
        provider.generate("p", [msg.user("hi")], [])
    assert excinfo.value.kind == "provider_error"
    assert "bad request" in str(excinfo.value)
    assert DEFAULT_MODEL in str(excinfo.value)


def test_network_failure_is_a_provider_error(provider, monkeypatch):
    def boom(*_a, **_k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(openrouter_module.httpx, "post", boom)
    with pytest.raises(ProviderError) as excinfo:
        provider.generate("p", [msg.user("hi")], [])
    assert excinfo.value.kind == "provider_error"
    assert "network error" in str(excinfo.value)


# --------------------------------------------------------------------------
# 5. The real loop: translated calls must drive agent.py end to end
# --------------------------------------------------------------------------


def test_the_translated_tool_calls_drive_the_real_agent_loop(seeded, captured, monkeypatch):
    provider = OpenRouterProvider(api_key=KEY)
    monkeypatch.setattr(agent, "get_provider", lambda: provider)

    captured["queue"].extend(
        [tool_call_reply(("call_1", "get_categories", {})), text_reply("عندنا T-Shirts و Hoodies")]
    )
    first = agent.run_turn(seeded, CHANNEL, WHO, "عندكم إيه؟", provider=provider)
    assert first.tool_calls == ["get_categories"]

    captured["queue"].append(text_reply("عندنا T-Shirts و Hoodies"))
    second = agent.run_turn(seeded, CHANNEL, WHO, "تمام", provider=provider)
    assert second.text == "عندنا T-Shirts و Hoodies"

    # The follow-up request carried the tool result back in OpenAI's shape.
    messages = captured["sent"][1]["body"]["messages"]
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["name"] == "get_categories"
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert '"categories"' in tool_messages[0]["content"]



# --------------------------------------------------------------------------
# 6. Voice notes and photos -- same endpoint, same model, same key as chat
# --------------------------------------------------------------------------


def test_voice_and_vision_are_always_declared_supported(provider):
    """Both media paths run on the key/model chat already needed, so nothing
    gates them: the runtime never has to hand a voice note or photo to a
    person for want of a second credential."""
    assert provider.supports_audio is True
    assert provider.supports_vision is True


def test_transcription_posts_an_input_audio_part_to_the_chat_endpoint(captured, provider):
    captured["queue"].append(text_reply("عايز هودي أسود مقاس لارج"))

    text = provider.transcribe(b"ogg-bytes", "audio/ogg", hint="سياق المحادثة")

    assert text == "عايز هودي أسود مقاس لارج"
    sent = captured["sent"][0]
    assert sent["url"] == f"{openrouter_module.BASE_URL}/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    body = sent["body"]
    # The transcript rides the media model, not the conversation one.
    assert body["model"] == DEFAULT_MEDIA_MODEL
    assert body["temperature"] == 0.0
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "حرفياً" in content[0]["text"]
    # The hint is soft context appended to the instruction, not a command.
    assert "سياق المحادثة: سياق المحادثة" in content[0]["text"]
    audio_part = content[1]
    assert audio_part["type"] == "input_audio"
    assert audio_part["input_audio"]["data"] == base64.b64encode(b"ogg-bytes").decode("ascii")
    assert audio_part["input_audio"]["format"] == "ogg"
    # Media is read outside the customer's turn, so it may wait at least as
    # long as chat -- a floor of 60s, not a raise above whatever chat uses.
    assert sent["timeout"] == provider.media_timeout >= provider.timeout


def test_transcription_without_a_hint_sends_the_instruction_alone(captured, provider):
    captured["queue"].append(text_reply("تمام"))
    provider.transcribe(b"ogg-bytes", "audio/ogg")
    content = captured["sent"][0]["body"]["messages"][0]["content"]
    assert "سياق المحادثة:" not in content[0]["text"]


@pytest.mark.parametrize(
    ("mime", "fmt"),
    [
        ("audio/ogg", "ogg"),
        ("audio/opus", "ogg"),
        ("audio/mpeg", "mp3"),
        ("audio/mp4", "mp4"),
        ("audio/m4a", "m4a"),
        ("audio/aac", "aac"),
        ("audio/amr", "amr"),
        ("audio/x-wav", "wav"),
        ("audio/webm", "webm"),
        ("audio/flac", "flac"),
    ],
)
def test_mime_types_map_to_wire_formats(captured, provider, mime, fmt):
    captured["queue"].append(text_reply("كلام"))
    provider.transcribe(b"bytes", mime)
    content = captured["sent"][0]["body"]["messages"][0]["content"]
    assert content[1]["input_audio"]["format"] == fmt


def test_unrecognised_mime_falls_back_to_ogg_format(captured, provider):
    captured["queue"].append(text_reply("كلام"))
    provider.transcribe(b"bytes", "audio/x-something-weird")
    content = captured["sent"][0]["body"]["messages"][0]["content"]
    assert content[1]["input_audio"]["format"] == "ogg"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("عايز هودي أسود", "عايز هودي أسود"),
        ('"عايز هودي أسود"', "عايز هودي أسود"),
        ("«عايز هودي أسود»", "عايز هودي أسود"),
        ("```\nعايز هودي أسود\n```", "عايز هودي أسود"),
        ("```text\nعايز هودي أسود\n```", "عايز هودي أسود"),
        ("  عايز هودي أسود  ", "عايز هودي أسود"),
    ],
)
def test_wrapping_quotes_and_fences_are_stripped_from_the_transcript(captured, provider, raw, expected):
    captured["queue"].append(text_reply(raw))
    assert provider.transcribe(b"bytes", "audio/ogg") == expected


def test_unintelligible_audio_transcribes_to_an_empty_string(captured, provider):
    """Same contract gemini.py's transcribe follows: nothing understandable,
    nothing transcribed -- runtime.py then hands the voice note to a person."""
    captured["queue"].append(text_reply("(غير مفهوم)"))
    assert provider.transcribe(b"bytes", "audio/ogg") == ""


def test_a_transcript_that_hit_the_ceiling_goes_to_a_person_not_into_the_turn(
    captured, provider, caplog
):
    """Half of what the customer said is worse than none of it.

    A cut-off transcript does not read as broken, it reads as a *shorter
    message*, and the whole turn is then built on it -- "عايز الهودي الأسود
    لارج بس لو مش متاح خليه ميديم" cut after "الأسود" is a different order.
    Everything downstream treats this string as the customer's own words, so
    there is nowhere further down to catch it. Empty is the documented "hand
    it to a person" signal, and a person listening to the voice note is
    exactly right: the words exist, we just could not write them all down.
    """
    captured["queue"].append(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "عايز الهودي الأسود"},
                    "finish_reason": "length",
                }
            ]
        }
    )
    with caplog.at_level(logging.WARNING):
        assert provider.transcribe(b"bytes", "audio/ogg") == ""
    assert "ceiling" in caplog.text


def test_a_finished_transcript_is_returned_whatever_its_length(captured, provider):
    """The guard keys on `finish_reason`, never on the text."""
    captured["queue"].append(text_reply("عايز الهودي الأسود لارج " * 40))
    assert provider.transcribe(b"bytes", "audio/ogg").startswith("عايز الهودي")


def test_the_transcription_budget_fits_a_long_voice_note(captured, provider):
    """Arabic runs two to three tokens a word here, and a customer describing
    an order and an address in one recording overran the old 1024."""
    captured["queue"].append(text_reply("تمام"))
    provider.transcribe(b"bytes", "audio/ogg")
    assert captured["sent"][0]["body"]["max_tokens"] == 4096


def test_transcription_never_sends_the_key_in_the_body(captured, provider):
    captured["queue"].append(text_reply("تمام"))
    provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert KEY not in json.dumps(captured["sent"][0]["body"], ensure_ascii=False)


@pytest.mark.parametrize("status", [401, 403])
def test_transcription_auth_rejection_names_its_kind(captured, provider, status):
    captured["queue"].append(FakeResponse({}, status_code=status, text="invalid key"))
    with pytest.raises(ProviderError) as excinfo:
        provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert excinfo.value.kind == "auth"
    assert KEY[-4:] in str(excinfo.value)  # masked, never the whole key
    assert KEY not in str(excinfo.value)


def test_transcription_rate_limit_names_its_kind(captured, provider):
    captured["queue"].append(FakeResponse({}, status_code=429, text="slow down"))
    with pytest.raises(ProviderError) as excinfo:
        provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert excinfo.value.kind == "rate_limit"
    # The media model, because that is the one the call actually ran on.
    assert DEFAULT_MEDIA_MODEL in str(excinfo.value)


def test_transcription_other_errors_keep_the_response_body(captured, provider):
    captured["queue"].append(FakeResponse({}, status_code=500, text='{"error":{"message":"upstream"}}'))
    with pytest.raises(ProviderError) as excinfo:
        provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert excinfo.value.kind == "provider_error"
    assert "upstream" in str(excinfo.value)
    assert DEFAULT_MEDIA_MODEL in str(excinfo.value)


def test_transcription_network_failure_is_a_provider_error(provider, monkeypatch):
    def boom(*_a, **_k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(openrouter_module.httpx, "post", boom)
    with pytest.raises(ProviderError) as excinfo:
        provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert excinfo.value.kind == "provider_error"
    assert "network error" in str(excinfo.value)


def test_vision_runs_on_the_media_model_when_one_is_configured(captured, provider, monkeypatch):
    """LLM_MEDIA_MODEL splits the *model id* and nothing else.

    This file used to assert the opposite -- that media must run on whatever
    chat runs on. That held only while one model was good at both. The model
    best at reading Egyptian Arabic off a voice note is not the one best at
    the tool loop, and pinning both to one id meant picking a chat model with
    no audio endpoint silently turned every voice note into a handoff. The
    key, the transport and the error mapping are still chat's.
    """
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(settings, openrouter_api_key=KEY, llm_media_model="openai/gpt-4o"),
    )
    provider = OpenRouterProvider()
    assert provider.model == DEFAULT_MODEL

    shortlist = [{"product_id": "wanas-hoodie", "name": "WANAS Hoodie", "category": "Hoodies", "colors": ["أسود"]}]
    captured["queue"].append(
        text_reply(
            json.dumps({"product_id": "wanas-hoodie", "confidence": 0.9, "description": "هودي أسود", "is_garment": True})
        )
    )

    reading = provider.inspect_image(b"jpeg-bytes", "image/jpeg", catalog=shortlist)

    assert reading.product_id == "wanas-hoodie"
    assert reading.confidence == 0.9
    assert reading.is_garment is True

    sent = captured["sent"][0]
    # Same endpoint, same key, same call shape -- only the model id differs.
    assert sent["url"] == f"{openrouter_module.BASE_URL}/chat/completions"
    assert sent["body"]["model"] == "openai/gpt-4o"
    assert provider.model == DEFAULT_MODEL
    content = sent["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "wanas-hoodie" in content[0]["text"]
    image_part = content[1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_media_falls_back_to_a_model_that_can_hear_not_to_the_chat_model(captured, monkeypatch):
    """Unset must NOT mean "same as chat".

    It used to, and that was safe only while the chat default could also read
    audio. The conversation model is now GLM, which has no audio endpoint at
    all -- so falling back to it would hand every unconfigured deployment the
    exact failure LLM_MEDIA_MODEL was added to fix: OpenRouter answers 404,
    media.py hands off to a person, and nothing says why.
    """
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(settings, openrouter_api_key=KEY, llm_media_model=""),
    )
    provider = OpenRouterProvider(model="z-ai/glm-5.3-flash")
    assert provider._media_model() == DEFAULT_MEDIA_MODEL

    captured["queue"].append(text_reply("مرحبا"))
    provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert captured["sent"][0]["body"]["model"] == DEFAULT_MEDIA_MODEL
    # Chat is untouched by the media default.
    assert provider.model == "z-ai/glm-5.3-flash"


def test_the_two_defaults_are_different_models(captured):
    """The whole point of the split, stated once so it cannot drift back."""
    assert DEFAULT_MODEL != DEFAULT_MEDIA_MODEL
    provider = OpenRouterProvider(api_key=KEY)
    assert provider.model == DEFAULT_MODEL
    assert provider._media_model() == DEFAULT_MEDIA_MODEL


def test_transcription_uses_the_media_model_not_the_chat_model(captured, monkeypatch):
    """The bug this setting exists for: a chat model with no audio endpoint."""
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(
            settings, openrouter_api_key=KEY, llm_media_model="google/gemini-3.1-flash-lite"
        ),
    )
    provider = OpenRouterProvider(model="z-ai/glm-5.3-flash")

    captured["queue"].append(text_reply("عايز الهودي الأسود"))
    assert provider.transcribe(b"ogg-bytes", "audio/ogg") == "عايز الهودي الأسود"

    body = captured["sent"][0]["body"]
    assert body["model"] == "google/gemini-3.1-flash-lite"
    assert body["messages"][0]["content"][1]["type"] == "input_audio"
    # Chat is untouched by the media setting.
    assert provider.model == "z-ai/glm-5.3-flash"


def test_a_model_with_no_audio_endpoint_says_so_by_kind(captured, monkeypatch):
    """404 "no endpoints support input audio" is a configuration mistake, and
    naming it `unsupported` is what tells a reader why every voice note
    suddenly became a handoff."""
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(settings, openrouter_api_key=KEY, llm_media_model="z-ai/glm-5.3-flash"),
    )
    provider = OpenRouterProvider(model="z-ai/glm-5.3-flash")
    captured["queue"].append(
        FakeResponse(
            {"error": {"message": "No endpoints found that support input audio"}}, status_code=404
        )
    )

    with pytest.raises(ProviderError) as excinfo:
        provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert excinfo.value.kind == "unsupported"


def test_the_size_chart_reader_follows_the_media_model_too(captured, monkeypatch):
    """It is a vision call like the others; two pictures must not be read by
    two different models."""
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(
            settings, openrouter_api_key=KEY, llm_media_model="google/gemini-3.1-flash-lite"
        ),
    )
    provider = OpenRouterProvider(model="z-ai/glm-5.3-flash")
    captured["queue"].append(
        text_reply(
            json.dumps(
                {
                    "measurements": [{"key": "width", "label_en": "Width", "label_ar": "العرض"}],
                    "sizes": {"S": {"width": 54}},
                    "unit": "cm",
                    "confidence": 0.9,
                    "notes": "",
                }
            )
        )
    )

    provider.read_size_chart(b"png-bytes", "image/png", sizes=["S"])
    assert captured["sent"][0]["body"]["model"] == "google/gemini-3.1-flash-lite"


def test_inspect_image_discards_a_product_id_the_shop_does_not_have(captured, provider):
    captured["queue"].append(
        text_reply(
            json.dumps(
                {"product_id": "not-a-real-product", "confidence": 0.8, "description": "شيء ما", "is_garment": True}
            )
        )
    )
    reading = provider.inspect_image(b"bytes", "image/jpeg", catalog=[{"product_id": "wanas-hoodie", "name": "x"}])
    assert reading.product_id is None


def test_inspect_image_rejects_a_non_json_reply(captured, provider):
    captured["queue"].append(text_reply("مش JSON خالص"))
    with pytest.raises(ProviderError) as excinfo:
        provider.inspect_image(b"bytes", "image/jpeg", catalog=[])
    assert "not JSON" in str(excinfo.value)


@pytest.mark.parametrize("status", [401, 403])
def test_vision_auth_rejection_names_its_kind(captured, provider, status):
    captured["queue"].append(FakeResponse({}, status_code=status, text="invalid key"))
    with pytest.raises(ProviderError) as excinfo:
        provider.inspect_image(b"bytes", "image/jpeg", catalog=[])
    assert excinfo.value.kind == "auth"


# --------------------------------------------------------------------------
# 7. classify_comment
# --------------------------------------------------------------------------


def test_classify_comment_reads_the_category(captured, provider):
    captured["queue"].append(text_reply(json.dumps({"category": "price"})))
    result = provider.classify_comment("بكام الهودي ده؟")
    assert result.category == "price"

    sent = captured["sent"][0]
    assert sent["body"]["response_format"] == {"type": "json_object"}
    assert "بكام الهودي ده؟" in sent["body"]["messages"][0]["content"]


def test_classify_comment_coerces_an_unknown_category_to_other(captured, provider):
    """Unrecognised no longer means silence: `other` answers politely and asks,
    which is the safe thing to say to a real person whose comment the model
    could not place."""
    captured["queue"].append(text_reply(json.dumps({"category": "sarcastic"})))
    assert provider.classify_comment("...").category == "other"


def test_classify_comment_maps_the_retired_names_forward(captured, provider):
    """A model pinned to the old prompt still routes somewhere sensible."""
    for legacy in ("important", "neither"):
        captured["queue"].append(text_reply(json.dumps({"category": legacy})))
        assert provider.classify_comment("...").category == "other"


def test_classify_comment_rejects_a_non_json_reply(captured, provider):
    captured["queue"].append(text_reply("مش JSON خالص"))
    with pytest.raises(ProviderError) as excinfo:
        provider.classify_comment("...")
    assert "not JSON" in str(excinfo.value)


def test_classify_comment_offers_every_category(provider):
    """The prompt and the accepted set have to agree: a category the prompt
    never describes is one the model never returns, and a category the prompt
    describes but the set rejects is silently coerced away."""
    from assistant.providers.base import COMMENT_CATEGORIES

    rendered = provider._COMMENT_INSTRUCTION.format(comment="x")
    for category in COMMENT_CATEGORIES:
        assert f'"{category}"' in rendered
    assert len(COMMENT_CATEGORIES) == 12


def test_classify_comment_reads_every_category(captured, provider):
    from assistant.providers.base import COMMENT_CATEGORIES

    for category in COMMENT_CATEGORIES:
        captured["queue"].append(text_reply(json.dumps({"category": category})))
        assert provider.classify_comment("...").category == category


def test_classify_comment_falls_back_to_the_chat_model(captured, monkeypatch):
    """Empty by default, so today's behaviour is bit-for-bit unchanged."""
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(settings, comment_classifier_model=""),
    )
    provider = OpenRouterProvider(api_key=KEY, model="chat/model-1")
    captured["queue"].append(text_reply(json.dumps({"category": "important"})))
    provider.classify_comment("بكام؟")

    assert captured["sent"][0]["body"]["model"] == "chat/model-1"


def test_a_classifier_model_decouples_the_public_surface_from_the_chat_model(
    captured, monkeypatch
):
    """Not a cost setting -- the call rounds to nothing. Without it, upgrading
    the chat model silently changes classification on a live public surface,
    and one model being pulled takes chat and comments down together."""
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(settings, comment_classifier_model="cheap/classifier-1"),
    )
    provider = OpenRouterProvider(api_key=KEY, model="chat/model-1")
    captured["queue"].append(text_reply(json.dumps({"category": "spam"})))
    provider.classify_comment("bit.ly/free-followers")

    assert captured["sent"][0]["body"]["model"] == "cheap/classifier-1"
    # ...and nothing else about the call moved: still JSON-only, no tools.
    assert captured["sent"][0]["body"]["max_tokens"] == 64
    assert captured["sent"][0]["body"]["temperature"] == 0.0
    assert "tools" not in captured["sent"][0]["body"]


def test_the_classifier_model_env_var_reaches_settings(monkeypatch):
    monkeypatch.setenv("COMMENT_CLASSIFIER_MODEL", "cheap/classifier-1")
    assert load_settings().comment_classifier_model == "cheap/classifier-1"


def test_vision_rate_limit_names_its_kind(captured, provider):
    captured["queue"].append(FakeResponse({}, status_code=429, text="slow down"))
    with pytest.raises(ProviderError) as excinfo:
        provider.inspect_image(b"bytes", "image/jpeg", catalog=[])
    assert excinfo.value.kind == "rate_limit"


def test_the_chat_budget_leaves_room_for_a_reasoning_model(captured, provider):
    """1024 was the reply's budget on a model that does not reason. On one
    that does, the same number is reasoning *plus* reply, and GLM was measured
    spending all of it on one question in six -- finish_reason=length, no
    content, no tool calls, and the customer gets the generic apology.

    4096 fixed the *empty* reply and left the truncated one: an order summary
    behind a few hundred reasoning tokens can still stop mid-sentence. The
    ceiling is headroom; `agent.run_turn` refusing to send a truncated reply
    is the guarantee.
    """
    provider.generate("p", [msg.user("الشحن كام؟")], [])
    assert captured["sent"][0]["body"]["max_tokens"] == 8192
    assert OpenRouterProvider.CHAT_MAX_TOKENS == 8192


def test_an_empty_reply_is_reported_with_its_finish_reason(captured, provider):
    """The signal that says *why* a turn produced nothing. Without it, a
    budget exhausted by reasoning is indistinguishable from a content filter.
    """
    captured["queue"].append(
        {"choices": [{"message": {"role": "assistant", "content": ""},
                      "finish_reason": "length"}]}
    )
    reply = provider.generate("p", [msg.user("hi")], [])
    assert not reply.text
    assert not reply.tool_calls
    assert reply.finish_reason == "length"


# --------------------------------------------------------------------------
# 7. Which serving stack answers, and what it is told
#
# The garbled-Arabic bug. One model id is not one model: OpenRouter hosts
# `z-ai/glm-5.3-flash` on twenty-three upstream providers at three
# quantizations and load-balances between them per request (six identical
# requests were measured landing on three different ones). By default an
# upstream that does not implement `temperature` is sent it anyway and
# silently drops it, so a reply meant to be sampled at 0.3 was sampled at that
# stack's own default. Most replies survived that; some came back
# grammatically broken or answering a question nobody asked.
# --------------------------------------------------------------------------


def test_the_payload_pins_which_upstreams_may_answer(captured, provider):
    """`require_parameters` is the correctness half: it is what makes
    temperature a setting rather than a suggestion."""
    provider.generate("p", [msg.user("الشحن كام؟")], [])

    routing = captured["sent"][0]["body"]["provider"]
    assert routing["require_parameters"] is True
    assert routing["order"] == ["z-ai", "deepinfra", "novita"]
    # fp8 and up. An endpoint that will not say what precision it runs at is
    # not one to put a customer's Arabic through.
    assert "unknown" not in routing["quantizations"]
    assert "fp8" in routing["quantizations"]
    # Excluding bad stacks must not turn a good one into a single point of
    # failure: anything that survives the filter may still answer.
    assert routing["allow_fallbacks"] is True


def test_the_temperature_the_routing_protects_is_still_sent(captured, provider):
    provider.generate("p", [msg.user("الشحن كام؟")], [])
    assert captured["sent"][0]["body"]["temperature"] == 0.3


def test_routing_is_configurable_and_can_be_turned_off(captured, monkeypatch):
    """A deployment that clears both variables gets OpenRouter's own default
    back, unchanged -- the filter is a decision, not a hardcoding."""
    monkeypatch.setattr(
        openrouter_module,
        "settings",
        dataclasses.replace(
            settings,
            openrouter_api_key=KEY,
            openrouter_providers=(),
            openrouter_quantizations=(),
        ),
    )
    OpenRouterProvider(api_key=KEY).generate("p", [msg.user("hi")], [])
    assert "provider" not in captured["sent"][0]["body"]


def test_the_routing_env_vars_reach_settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDERS", " z-ai , together ")
    monkeypatch.setenv("OPENROUTER_QUANTIZATIONS", "fp8")
    loaded = load_settings()
    assert loaded.openrouter_providers == ("z-ai", "together")
    assert loaded.openrouter_quantizations == ("fp8",)


def test_an_empty_routing_var_means_empty_not_default(monkeypatch):
    """Unset and explicitly-blank are different answers and have to stay
    distinguishable: blank is "do not constrain this"."""
    monkeypatch.setenv("OPENROUTER_PROVIDERS", "")
    assert load_settings().openrouter_providers == ()
    monkeypatch.delenv("OPENROUTER_PROVIDERS")
    assert load_settings().openrouter_providers == ("z-ai", "deepinfra", "novita")


# -- reasoning blocks across a tool loop ------------------------------------


def _reasoning_details() -> list[dict]:
    return [{"type": "reasoning.text", "text": "the customer asked for XL", "index": 0}]


def test_reasoning_details_come_back_on_the_reply(captured, provider):
    captured["queue"].append(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_details": _reasoning_details(),
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_variants", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    reply = provider.generate("p", [msg.user("عايز XL")], [])
    assert reply.signature == _reasoning_details()


def test_reasoning_details_are_replayed_with_the_turn_that_produced_them(captured, provider):
    """OpenRouter is explicit that a reasoning model needs its own blocks back
    for multi-turn tool calling. Without them the model gets its tool results
    with no record of why it asked for them, and answers the question it
    reconstructs rather than the one that was asked."""
    history = [
        msg.user("عايز XL"),
        msg.assistant(
            "",
            [msg.tool_call("call_1", "get_variants", {"product_id": "wanas-hoodie"})],
            signature=_reasoning_details(),
        ),
        msg.tool_results([msg.tool_result("call_1", "get_variants", {"variants": []})]),
    ]
    provider.generate("p", history, [])

    assistant = captured["sent"][0]["body"]["messages"][2]
    assert assistant["reasoning_details"] == _reasoning_details()


def test_a_foreign_signature_is_still_dropped(captured, provider):
    """A Gemini thought signature is a string. It means nothing on this
    protocol and must not be dressed up as a reasoning block."""
    history = [
        msg.user("عايز XL"),
        msg.assistant("", [msg.tool_call("call_1", "get_variants", {})], signature=SIG),
    ]
    provider.generate("p", history, [])

    assistant = captured["sent"][0]["body"]["messages"][2]
    assert "reasoning_details" not in assistant
    assert "sig-" not in json.dumps(captured["sent"][0]["body"]["messages"])


def test_a_reply_with_no_reasoning_carries_no_signature(captured, provider):
    captured["queue"].append(text_reply("تمام"))
    assert provider.generate("p", [msg.user("hi")], []).signature is None
