"""The OpenRouter provider.

No network anywhere: httpx is stubbed exactly as in test_gemini_provider.py,
because the point is the translation between the neutral message format and
the OpenAI-compatible wire shapes, the error kinds, and the decision of when
voice/photo support is declared -- not OpenRouter's uptime or its models'.

The integration test in section 5 drives the real agent loop through this
provider, which is what proves the tool-call translation actually matches
what agent.py consumes. Section 6 covers voice and photos -- both ride the
same chat/completions endpoint and the same single model as chat (an
input_audio / image_url content part respectively), keyed only by the
OpenRouter key. There is no Gemini anywhere in this provider; that stays
exercised through LLM_PROVIDER=gemini in test_gemini_provider.py.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging

import httpx
import pytest

from backend.config import load_settings, settings
from chatbot import agent
from chatbot import messages as msg
from chatbot.providers import build_provider
from chatbot.providers import openrouter as openrouter_module
from chatbot.providers.base import ProviderError
from chatbot.providers.openrouter import (
    DEFAULT_MODEL,
    OpenRouterProvider,
)
from chatbot.tools.base import tool_specs

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
    # The transcript rides the conversation model itself.
    assert body["model"] == DEFAULT_MODEL
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
    # Media is read outside the customer's turn; it may wait longer than chat.
    assert sent["timeout"] == provider.media_timeout > provider.timeout


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
    assert DEFAULT_MODEL in str(excinfo.value)


def test_transcription_other_errors_keep_the_response_body(captured, provider):
    captured["queue"].append(FakeResponse({}, status_code=500, text='{"error":{"message":"upstream"}}'))
    with pytest.raises(ProviderError) as excinfo:
        provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert excinfo.value.kind == "provider_error"
    assert "upstream" in str(excinfo.value)
    assert DEFAULT_MODEL in str(excinfo.value)


def test_transcription_network_failure_is_a_provider_error(provider, monkeypatch):
    def boom(*_a, **_k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(openrouter_module.httpx, "post", boom)
    with pytest.raises(ProviderError) as excinfo:
        provider.transcribe(b"ogg-bytes", "audio/ogg")
    assert excinfo.value.kind == "provider_error"
    assert "network error" in str(excinfo.value)


def test_vision_runs_on_the_same_shared_model_as_chat(captured, provider, monkeypatch):
    """No separate vision model exists any more: LLM_MEDIA_MODEL must not
    split the paths, and both calls name exactly what chat names."""
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
    assert sent["url"] == f"{openrouter_module.BASE_URL}/chat/completions"
    assert sent["body"]["model"] == provider.model == DEFAULT_MODEL
    content = sent["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "wanas-hoodie" in content[0]["text"]
    image_part = content[1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


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


def test_vision_rate_limit_names_its_kind(captured, provider):
    captured["queue"].append(FakeResponse({}, status_code=429, text="slow down"))
    with pytest.raises(ProviderError) as excinfo:
        provider.inspect_image(b"bytes", "image/jpeg", catalog=[])
    assert excinfo.value.kind == "rate_limit"
