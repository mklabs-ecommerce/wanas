"""The Gemini provider's sharp edges.

Each of these corresponds to a failure mode that was hit for real on an
earlier build of this bot. No network: httpx is stubbed, because the point is
the translation and the recovery logic, not Google's uptime.

The thought-signature tests are the ones that matter most. That failure only
appears on the *second* tool call in a conversation, so a test that makes one
call passes while real use fails.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from assistant import agent, messages as msg, session as session_store
from assistant.providers import gemini as gemini_module
from assistant.providers.base import ProviderError
from assistant.providers.gemini import GeminiProvider, is_gemini_3, mask_key
from assistant.tools.base import tool_specs
from config.settings import settings

CHANNEL = "whatsapp"
WHO = "201000000001"

# Two different opaque blobs, so a test cannot pass by replaying the wrong one.
SIG_1 = "CvQBAdHtim9sig-for-the-first-call"
SIG_2 = "CvQBAdHtim9sig-for-the-second-call"


class FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


def function_call_reply(name: str, args: dict, signature: str | None = None, text: str = "") -> dict:
    part: dict = {"functionCall": {"name": name, "args": args}}
    if signature:
        part["thoughtSignature"] = signature
    parts = ([{"text": text}] if text else []) + [part]
    return {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}


def text_reply(text: str, signature: str | None = None) -> dict:
    part: dict = {"text": text}
    if signature:
        part["thoughtSignature"] = signature
    return {"candidates": [{"content": {"parts": [part]}, "finishReason": "STOP"}]}


@pytest.fixture()
def captured(monkeypatch):
    """Capture every request body the provider sends."""
    sent: list[dict] = []
    queue: list[FakeResponse] = []

    def fake_post(url, params=None, json=None, timeout=None, headers=None):
        sent.append({"url": url, "params": params or {}, "body": json})
        if not queue:
            return FakeResponse(text_reply("ok"))
        nxt = queue.pop(0)
        # Queue entries may be a bare response body or an explicit
        # FakeResponse when the status code matters.
        return nxt if isinstance(nxt, FakeResponse) else FakeResponse(nxt)

    monkeypatch.setattr(gemini_module.httpx, "post", fake_post)
    return {"sent": sent, "queue": queue}


@pytest.fixture()
def provider():
    return GeminiProvider(api_key="AQ.Ab-not-a-real-key", model="gemini-3.1-flash-lite", auto_resolve=False)


# --------------------------------------------------------------------------
# 1. Key format
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["AQ.Ab8RN6JoexsBT5jb", "AIzaSyC-old-style-key", "totally-custom-key"])
def test_any_key_format_is_accepted(key):
    """Newer keys are AQ.Ab...; anything that pattern-matches a prefix rejects
    a valid credential."""
    assert GeminiProvider(api_key=key, model="gemini-2.0-flash", auto_resolve=False).api_key == key


def test_only_an_absent_key_is_rejected():
    with pytest.raises(ProviderError) as excinfo:
        GeminiProvider(api_key="", model="gemini-2.0-flash")
    assert excinfo.value.kind == "auth"


@pytest.mark.parametrize(
    "key,expected",
    [("AQ.Ab8RN6Joexs1234", "…1234"), ("AIzaSyCabcd", "…abcd"), ("", "(unset)"), ("ab", "…")],
)
def test_masking_does_not_assume_a_shape(key, expected):
    assert mask_key(key) == expected


def test_gemini_key_env_alias_is_accepted(monkeypatch):
    """A key present under the name a Gemini-only .env already uses must not
    look like a missing key."""
    from config.settings import load_settings

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.Ab-alias-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    loaded = load_settings()
    assert loaded.llm_api_key == "AQ.Ab-alias-key"
    assert loaded.llm_model == "gemini-3.1-flash-lite"


# --------------------------------------------------------------------------
# 2. Model aliases going stale
# --------------------------------------------------------------------------


@pytest.fixture()
def model_list(monkeypatch):
    listed = {"models": []}

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(listed)

    monkeypatch.setattr(gemini_module.httpx, "get", fake_get)
    return listed


def _models(*names):
    return [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in names]


def test_available_models_lists_only_generatecontent_models(model_list, provider):
    model_list["models"] = _models("gemini-2.0-flash", "gemini-2.5-flash-lite") + [
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}
    ]
    assert provider.available_models() == ["gemini-2.0-flash", "gemini-2.5-flash-lite"]


def test_a_configured_model_that_exists_is_kept(model_list, provider):
    model_list["models"] = _models("gemini-3.1-flash-lite", "gemini-2.0-flash")
    assert provider.resolve_model() == "gemini-3.1-flash-lite"


def test_a_configured_model_that_is_gone_falls_back_and_says_so(model_list, provider, caplog):
    model_list["models"] = _models("gemini-2.5-flash-lite", "gemini-2.5-flash")
    with caplog.at_level("INFO"):
        chosen = provider.resolve_model()
    assert chosen == "gemini-2.5-flash-lite"
    # Silently switching models is its own kind of confusing.
    assert "is not available" in caplog.text
    assert "resolved to 'gemini-2.5-flash-lite'" in caplog.text


def test_an_unset_model_is_resolved_rather_than_hardcoded(model_list):
    model_list["models"] = _models("gemini-9-experimental", "gemini-2.0-flash")
    picked = GeminiProvider(api_key="k", model="")
    assert picked.auto_resolve is True
    assert picked.resolve_model() == "gemini-2.0-flash"


def test_a_key_with_no_usable_model_is_an_auth_problem(model_list, provider):
    model_list["models"] = []
    with pytest.raises(ProviderError) as excinfo:
        provider.resolve_model()
    assert excinfo.value.kind == "auth"


def test_a_404_is_retried_once_against_the_live_list(captured, model_list, monkeypatch):
    """A hardcoded name deprecated out from under the code looks like a broken
    deployment otherwise."""
    model_list["models"] = _models("gemini-2.5-flash-lite")
    provider = GeminiProvider(api_key="k", model="gemini-2.5-flash", auto_resolve=True)
    provider._resolved = True  # pretend it resolved cleanly at boot, then went away

    captured["queue"].extend(
        [FakeResponse({}, status_code=404, text="model not found"), FakeResponse(text_reply("أهلاً"))]
    )
    reply = provider.generate("prompt", [msg.user("hi")], [])

    assert reply.text == "أهلاً"
    assert provider.model == "gemini-2.5-flash-lite"
    assert "gemini-2.5-flash:generateContent" in captured["sent"][0]["url"]
    assert "gemini-2.5-flash-lite:generateContent" in captured["sent"][1]["url"]


def test_a_429_names_the_model_so_it_reads_as_quota_not_a_bug(captured, provider):
    captured["queue"].append(FakeResponse({}, status_code=429, text="RESOURCE_EXHAUSTED"))
    with pytest.raises(ProviderError) as excinfo:
        provider.generate("prompt", [msg.user("hi")], [])
    assert excinfo.value.kind == "rate_limit"
    assert "gemini-3.1-flash-lite" in str(excinfo.value)


def test_thinking_budget_is_not_sent_to_gemini_3(captured, provider):
    """`thinkingBudget: 0` is a 2.x field and Gemini 3 rejects it."""
    provider.generate("prompt", [msg.user("hi")], [])
    assert "thinkingConfig" not in captured["sent"][0]["body"]["generationConfig"]

    two_x = GeminiProvider(api_key="k", model="gemini-2.0-flash", auto_resolve=False)
    two_x.generate("prompt", [msg.user("hi")], [])
    assert captured["sent"][1]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


@pytest.mark.parametrize(
    "model,expected",
    [("gemini-3.1-flash-lite", True), ("models/gemini-3-pro", True), ("gemini-2.5-flash", False), ("", False)],
)
def test_gemini_3_detection(model, expected):
    assert is_gemini_3(model) is expected


# --------------------------------------------------------------------------
# 3. Thought signatures -- the one that only breaks on the second call
# --------------------------------------------------------------------------


def test_a_signature_on_a_tool_call_is_captured(captured, provider):
    captured["queue"].append(function_call_reply("view_cart", {}, signature=SIG_1))
    reply = provider.generate("prompt", [msg.user("cart")], [])
    assert reply.tool_calls[0]["signature"] == SIG_1


def test_a_signature_on_the_text_part_is_captured(captured, provider):
    captured["queue"].append(text_reply("أهلاً", signature=SIG_1))
    assert provider.generate("prompt", [msg.user("hi")], []).signature == SIG_1


def test_a_reply_with_no_signature_carries_none(captured, provider):
    captured["queue"].append(function_call_reply("view_cart", {}))
    reply = provider.generate("prompt", [msg.user("cart")], [])
    assert "signature" not in reply.tool_calls[0]
    assert reply.signature is None


def test_the_signature_is_replayed_on_the_next_request(captured, provider):
    """The actual contract: it goes back on the part it belonged to."""
    history = [
        msg.user("عايز الهودي"),
        msg.assistant("", [msg.tool_call("call_0", "get_variants", {"product_id": "x"}, SIG_1)]),
        msg.tool_results([msg.tool_result("call_0", "get_variants", {"variants": []})]),
    ]
    provider.generate("prompt", history, [])

    model_turn = next(c for c in captured["sent"][0]["body"]["contents"] if c["role"] == "model")
    part = model_turn["parts"][0]
    assert part["functionCall"]["name"] == "get_variants"
    assert part["thoughtSignature"] == SIG_1


def test_a_text_signature_is_replayed_on_its_own_part(captured, provider):
    history = [
        msg.user("هاي"),
        msg.assistant("ثانية واحدة", [msg.tool_call("call_1", "view_cart", {}, SIG_2)], signature=SIG_1),
        msg.tool_results([msg.tool_result("call_1", "view_cart", {"lines": []})]),
    ]
    provider.generate("prompt", history, [])

    parts = next(c for c in captured["sent"][0]["body"]["contents"] if c["role"] == "model")["parts"]
    assert parts[0]["text"] == "ثانية واحدة"
    assert parts[0]["thoughtSignature"] == SIG_1
    assert parts[1]["thoughtSignature"] == SIG_2


def test_multi_turn_tool_calling_replays_signatures_through_the_database(seeded, captured, monkeypatch):
    """The regression this whole item exists for.

    Two tool calls in one conversation, with the session written to and read
    back from the database in between -- which is where an opaque string would
    be dropped if the neutral format did not carry it. A single-call test
    passes either way.
    """
    provider = GeminiProvider(api_key="k", model="gemini-3.1-flash-lite", auto_resolve=False)
    monkeypatch.setattr(agent, "get_provider", lambda: provider)

    # Turn one: a tool call carrying SIG_1, then a plain answer.
    captured["queue"].extend(
        [function_call_reply("get_categories", {}, signature=SIG_1), text_reply("عندنا T-Shirts")]
    )
    first = agent.run_turn(seeded, CHANNEL, WHO, "عندكم إيه؟", provider=provider)
    assert first.tool_calls == ["get_categories"]

    # The signature is in the database, not just in memory.
    seeded.commit()
    seeded.expire_all()
    stored = session_store.load(seeded, CHANNEL, WHO)
    stored_call = next(m for m in stored if m.get("tool_calls"))["tool_calls"][0]
    assert stored_call["signature"] == SIG_1

    # Turn two: a second tool call, in a conversation that already has one.
    captured["queue"].extend(
        [function_call_reply("view_cart", {}, signature=SIG_2), text_reply("الشنطة فاضية")]
    )
    second = agent.run_turn(seeded, CHANNEL, WHO, "وشنطتي؟", provider=provider)
    assert second.text == "الشنطة فاضية"

    # The request that carried the *second* call must have replayed the first
    # signature -- this is the exact request the API rejects when it does not.
    third_request = captured["sent"][2]["body"]
    model_turns = [c for c in third_request["contents"] if c["role"] == "model"]
    signatures = [
        part["thoughtSignature"]
        for turn in model_turns
        for part in turn["parts"]
        if "thoughtSignature" in part
    ]
    assert SIG_1 in signatures, "the first turn's signature was dropped between turns"

    # And the fourth request replays both.
    fourth_request = captured["sent"][3]["body"]
    signatures = [
        part["thoughtSignature"]
        for turn in fourth_request["contents"]
        if turn["role"] == "model"
        for part in turn["parts"]
        if "thoughtSignature" in part
    ]
    assert signatures == [SIG_1, SIG_2]


def test_signatures_survive_session_trimming(seeded):
    """Trimming cuts at a user message, so a kept tool call keeps its
    signature and a dropped one takes its signature with it."""
    history = []
    for n in range(30):
        history.append(msg.user(f"u{n}"))
        history.append(msg.assistant("", [msg.tool_call(f"c{n}", "view_cart", {}, f"sig-{n}")]))
        history.append(msg.tool_results([msg.tool_result(f"c{n}", "view_cart", {})]))

    session_store.save(seeded, CHANNEL, WHO, history)
    reloaded = session_store.load(seeded, CHANNEL, WHO)

    for message in reloaded:
        for call in message.get("tool_calls") or []:
            assert call["signature"] == f"sig-{call['id'][1:]}"


# --------------------------------------------------------------------------
# 4. Diagnosing INVALID_ARGUMENT
# --------------------------------------------------------------------------


def test_payload_logging_is_off_by_default(captured, provider, caplog):
    assert settings.llm_debug_payload is False
    with caplog.at_level("DEBUG"):
        provider.generate("prompt", [msg.user("hi")], [])
    assert "payload" not in caplog.text


def test_payload_logging_shows_the_exact_request_body(captured, provider, monkeypatch, caplog):
    monkeypatch.setattr(
        gemini_module, "settings", dataclasses.replace(settings, llm_debug_payload=True)
    )
    with caplog.at_level("WARNING"):
        provider.generate("system prompt here", [msg.user("عايز هودي")], tool_specs()[:1])

    assert "payload" in caplog.text
    assert "عايز هودي" in caplog.text  # readable, not \u-escaped
    assert "functionDeclarations" in caplog.text


def test_the_api_key_is_never_in_the_logged_payload(captured, monkeypatch, caplog):
    monkeypatch.setattr(
        gemini_module, "settings", dataclasses.replace(settings, llm_debug_payload=True)
    )
    secret = "AQ.Ab-super-secret-key"
    provider = GeminiProvider(api_key=secret, model="gemini-2.0-flash", auto_resolve=False)
    with caplog.at_level("WARNING"):
        provider.generate("prompt", [msg.user("hi")], [])

    assert secret not in caplog.text
    # It travels as a query parameter, which is not part of the logged URL.
    assert captured["sent"][0]["params"]["key"] == secret


def test_an_invalid_argument_keeps_the_response_body_in_the_error(captured, provider):
    captured["queue"].append(
        FakeResponse({}, status_code=400, text='{"error":{"status":"INVALID_ARGUMENT"}}')
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.generate("prompt", [msg.user("hi")], [])
    assert "INVALID_ARGUMENT" in str(excinfo.value)
    assert "gemini-3.1-flash-lite" in str(excinfo.value)


# --------------------------------------------------------------------------
# 5. Logged loudly, generic to the customer
# --------------------------------------------------------------------------


def test_an_unexpected_provider_crash_still_answers_the_customer(seeded, caplog):
    """An exception escaping the agent means the WhatsApp adapter sends
    nothing at all -- silent from the customer's side."""

    class Exploding:
        name = "exploding"

        def generate(self, *_a, **_k):
            raise ValueError("something the provider never classified")

    with caplog.at_level("ERROR"):
        reply = agent.run_turn(seeded, CHANNEL, WHO, "هاي", provider=Exploding())

    assert reply.text == agent.GENERIC_FAILURE
    assert reply.error == "provider_crash"
    # Logged with a traceback...
    assert "unexpected failure" in caplog.text
    assert "ValueError" in caplog.text
    # ...but nothing about it reaches the customer.
    assert "something the provider never classified" not in reply.text


def test_raw_provider_text_never_reaches_the_customer_by_default(seeded, captured, provider):
    assert settings.chatbot_debug is False
    captured["queue"].append(FakeResponse({}, status_code=400, text="INVALID_ARGUMENT: contents[0].parts"))
    reply = agent.run_turn(seeded, CHANNEL, WHO, "هاي", provider=provider)
    assert reply.text == agent.GENERIC_FAILURE
    assert "INVALID_ARGUMENT" not in reply.text


def test_committed_env_example_ships_both_debug_flags_off():
    """The deployed-with-DEBUG-on failure has to be hard to reach by accident."""
    from config.settings import PROJECT_ROOT

    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CHATBOT_DEBUG=0" in example
    assert "LLM_DEBUG_PAYLOAD=0" in example
    assert "LLM_API_KEY=\n" in example  # committed empty, never with a value


# --------------------------------------------------------------------------
# 6. Option ordering (catalog, not Gemini) -- confirming it cannot regress
# --------------------------------------------------------------------------


def test_the_size_axis_is_identified_by_its_values_not_its_position():
    """Shopify does not guarantee option1 is size. The merge map classifies by
    inspecting values, and raises rather than degrading."""
    import importlib.util

    from config.settings import PROJECT_ROOT

    spec = importlib.util.spec_from_file_location("merge_catalog", PROJECT_ROOT / "data" / "merge_catalog.py")
    merge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(merge)

    size_first = [{"option1": "M", "option2": "Black"}]
    size_second = [{"option1": "Black", "option2": "M"}]
    assert merge.classify_options("h", size_first) == ("option1", "option2")
    # The reversed product resolves to the same axes, by value not position.
    assert merge.classify_options("h", size_second) == ("option2", "option1")


def test_an_unknown_size_vocabulary_raises_rather_than_silently_nulling():
    import importlib.util

    from config.settings import PROJECT_ROOT

    spec = importlib.util.spec_from_file_location("merge_catalog", PROJECT_ROOT / "data" / "merge_catalog.py")
    merge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(merge)

    with pytest.raises(SystemExit):
        merge.classify_options("h", [{"option1": "Black", "option2": "Olive"}])


def test_the_runtime_never_reads_an_option_position(seeded):
    """The app reads explicit size/color/length columns; nothing downstream
    could regress if the catalog is re-merged."""
    from domain.models import Variant

    variant = seeded.get(Variant, "worker-jacket-m-black-long")
    if variant is None:  # id shape differs; take any Worker Jacket variant
        variant = next(v for v in seeded.query(Variant).all() if v.length)
    assert variant.size and variant.color and variant.length
    assert not hasattr(variant, "option1")
