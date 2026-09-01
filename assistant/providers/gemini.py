"""Gemini provider.

Talks to the REST endpoint with httpx rather than the vendor SDK -- one less
dependency, and it keeps every vendor detail inside this file where the
boundary says it belongs.

Quirks absorbed here, none of which leak upward:

* Tool parameters must be an OpenAPI-ish subset, and a function with no
  parameters must omit the `parameters` key entirely rather than send an empty
  object, which the API rejects.
* Tool calls and their results are `functionCall` / `functionResponse` parts
  inside `model` and `user` turns, not roles of their own.
* **Thought signatures.** Gemini 3 attaches an opaque `thoughtSignature` to the
  parts it produces, and rejects the *next* request if the signature is not
  sent back with the turn it belonged to. This breaks on the second tool call
  in a conversation, never the first, so it looks like a random failure rather
  than a missing field. Captured in `_parse`, replayed in `_contents`, and
  carried through the database as an opaque string the rest of the system
  never inspects.
* **Thinking configuration differs by generation.** `thinkingBudget: 0` is a
  2.x setting; Gemini 3 rejects it. Omitted entirely for 3.x rather than
  guessing at the replacement field.
* **Model names go stale.** A hardcoded name can be deprecated out from under
  the code (404) or resolve to a model with effectively no free quota (429).
  `available_models()` asks the key what it can actually call, and a 404 is
  retried once against a re-resolved model.
* **No assumption about the API key's shape.** Newer keys are `AQ.Ab...`
  rather than `AIzaSy...`; anything that pattern-matches a prefix rejects a
  valid credential.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

from assistant.messages import ASSISTANT, TOOL_RESULTS, USER
from assistant.providers.base import (
    COMMENT_CATEGORIES,
    CommentClassification,
    ImageReading,
    LLMProvider,
    ModelReply,
    ProviderError,
)
from config.settings import settings

log = logging.getLogger("wanas.provider.gemini")

BASE_URL = "https://generativelanguage.googleapis.com"
API_VERSION = "v1beta"

#: Tried in order when the configured model is not available to this key.
#: Deliberately conservative: flash-lite and flash carry the workable free
#: quotas, and `-latest` style aliases are avoided because they can resolve to
#: a model whose free tier is effectively zero -- which surfaces as a 429 that
#: reads like a bug rather than a quota decision.
PREFERRED_MODELS = (
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)


def mask_key(key: str) -> str:
    """Format-agnostic masking for log lines.

    Deliberately does not know or care what a key looks like: it shows the
    last four characters of whatever it is given, so `AQ.Ab...` and
    `AIzaSy...` both mask correctly and neither is ever printed in full.
    """
    if not key:
        return "(unset)"
    return f"…{key[-4:]}" if len(key) > 4 else "…"


def is_gemini_3(model: str) -> bool:
    return (model or "").lower().replace("models/", "").startswith("gemini-3")


class GeminiProvider(LLMProvider):
    name = "gemini"

    # Every Gemini model this code will resolve to reads audio and images on
    # the same `generateContent` endpoint the conversation already uses, so
    # there is no second vendor, no second key, and no second failure mode.
    supports_audio = True
    supports_vision = True

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        *,
        auto_resolve: bool | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = (model or settings.llm_model or "").replace("models/", "")
        self.timeout = timeout
        #: Media is read outside the customer's turn (the reply is already on
        #: its way to them), so it can afford to wait longer than a chat call
        #: that someone is sitting in front of.
        self.media_timeout = max(timeout, 60.0)
        #: Resolve against the live model list when the configured name is
        #: missing or turns out to be gone. Off in tests that pin a model.
        self.auto_resolve = (not self.model) if auto_resolve is None else auto_resolve
        self._resolved = False
        if not self.api_key:
            # No pattern check: presence is the only thing that can be
            # validated locally without rejecting a valid new-format key.
            raise ProviderError("LLM_API_KEY (or GEMINI_API_KEY) is not set", kind="auth")

    # -- model discovery --------------------------------------------------

    def available_models(self) -> list[str]:
        """What this key can actually call, generateContent-capable only."""
        try:
            response = httpx.get(
                f"{BASE_URL}/{API_VERSION}/models",
                params={"key": self.api_key, "pageSize": 200},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"could not list Gemini models: {exc}") from exc

        if response.status_code in (401, 403):
            raise ProviderError(
                f"auth rejected listing models ({response.status_code}) for key {mask_key(self.api_key)}",
                kind="auth",
            )
        if response.status_code >= 400:
            raise ProviderError(f"could not list Gemini models: {response.status_code} {response.text[:300]}")

        return [
            model["name"].replace("models/", "")
            for model in response.json().get("models", [])
            if "generateContent" in (model.get("supportedGenerationMethods") or [])
        ]


    def resolve_model(self, *, force: bool = False) -> str:
        """Pick a model that exists, and say out loud which one.

        Silently switching models is its own kind of confusing -- a reply that
        suddenly costs more or behaves differently with no record of why.
        """
        if self._resolved and not force:
            return self.model

        available = self.available_models()
        if not available:
            raise ProviderError("no generateContent-capable models are available to this key", kind="auth")

        if self.model and self.model in available and not force:
            chosen = self.model
        else:
            chosen = next(
                (name for name in PREFERRED_MODELS if name in available),
                # Nothing preferred is available: take a flash model if there
                # is one, otherwise whatever the key does have.
                next((name for name in available if "flash" in name), available[0]),
            )
            if self.model:
                log.warning(
                    "configured model %r is not available to key %s; using %r instead",
                    self.model,
                    mask_key(self.api_key),
                    chosen,
                )

        log.info("gemini model resolved to %r (key %s)", chosen, mask_key(self.api_key))
        self.model = chosen
        self._resolved = True
        return chosen

    # -- translation ------------------------------------------------------

    @staticmethod
    def _schema(spec) -> dict:
        declaration = {"name": spec.name, "description": spec.description}
        if spec.properties:
            declaration["parameters"] = {
                "type": "OBJECT",
                "properties": {
                    name: {k: v for k, v in prop.items() if k in {"description"}}
                    | {"type": prop.get("type", "string").upper()}
                    for name, prop in spec.properties.items()
                },
                "required": list(spec.required),
            }
        # A function with no arguments omits `parameters` entirely: an empty
        # object is rejected outright.
        return declaration

    def _contents(self, history: list[dict]) -> list[dict]:
        contents: list[dict] = []
        for message in history:
            role = message.get("role")
            if role == USER:
                contents.append({"role": "user", "parts": [{"text": message.get("content", "")}]})
            elif role == ASSISTANT:
                parts = []
                if message.get("content"):
                    text_part: dict = {"text": message["content"]}
                    # A signature on the text part is replayed too: Gemini 3
                    # can attach one to the reasoning that preceded the tool
                    # calls, not only to the calls themselves.
                    if message.get("signature"):
                        text_part["thoughtSignature"] = message["signature"]
                    parts.append(text_part)
                for call in message.get("tool_calls") or []:
                    part: dict = {
                        "functionCall": {"name": call["name"], "args": call.get("arguments") or {}}
                    }
                    if call.get("signature"):
                        # The whole point: without this the *next* request in
                        # the conversation is rejected for a missing
                        # signature, which only ever happens on the second
                        # tool call and reads like an intermittent fault.
                        part["thoughtSignature"] = call["signature"]
                    parts.append(part)
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif role == TOOL_RESULTS:
                parts = [
                    {
                        "functionResponse": {
                            "name": result["name"],
                            "response": {"result": result.get("content")},
                        }
                    }
                    for result in message.get("results") or []
                ]
                if parts:
                    contents.append({"role": "user", "parts": parts})
        return contents

    def _build_payload(self, system_prompt: str, history: list[dict], tools: list) -> dict:
        generation_config: dict = {"temperature": 0.3, "maxOutputTokens": 1024}
        if not is_gemini_3(self.model):
            # Short replies, latency over deliberation. `thinkingBudget` is a
            # 2.x field; Gemini 3 rejects it outright, so its thinking
            # configuration is left at the API default rather than guessed at.
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}

        payload: dict = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": self._contents(history),
            "generationConfig": generation_config,
        }
        if tools:
            payload["tools"] = [{"functionDeclarations": [self._schema(spec) for spec in tools]}]
        return payload

    # -- request ----------------------------------------------------------

    def generate(self, system_prompt: str, history: list[dict], tools: list) -> ModelReply:
        if self.auto_resolve and not self._resolved:
            self.resolve_model()

        payload = self._build_payload(system_prompt, history, tools)
        response = self._post(payload)

        if response.status_code == 404 and self.auto_resolve:
            # The configured model was deprecated out from under us. Re-resolve
            # once against what the key can actually call, then retry -- a 404
            # here otherwise looks like a broken deployment.
            log.warning("model %r returned 404; re-resolving against the live model list", self.model)
            self.resolve_model(force=True)
            payload = self._build_payload(system_prompt, history, tools)
            response = self._post(payload)

        if response.status_code == 429:
            # Naming the model matters: an alias that resolves to a
            # zero-free-quota model produces a 429 that reads like a bug.
            raise ProviderError(
                f"rate limited or out of quota on model {self.model!r}: {response.text[:300]}",
                kind="rate_limit",
            )
        if response.status_code in (401, 403):
            raise ProviderError(
                f"auth rejected ({response.status_code}) for key {mask_key(self.api_key)}: "
                f"{response.text[:300]}",
                kind="auth",
            )
        if response.status_code >= 400:
            # INVALID_ARGUMENT rarely names the offending field. The response
            # body is kept in the message, and LLM_DEBUG_PAYLOAD has already
            # logged exactly what was sent.
            raise ProviderError(
                f"gemini error {response.status_code} on model {self.model!r}: {response.text[:500]}"
            )

        return self._parse(response.json())

    def _post(self, payload: dict) -> httpx.Response:
        url = f"{BASE_URL}/{API_VERSION}/models/{self.model}:generateContent"

        if settings.llm_debug_payload:
            # Off by default. The API key travels as a query parameter, never
            # in the body, so the dumped payload carries no credential -- and
            # the URL is logged without its parameters for the same reason.
            log.warning("POST %s payload:\n%s", url, _debug_dump(payload))

        try:
            return httpx.post(
                url,
                params={"key": self.api_key},
                json=payload,
                timeout=self.timeout,
                headers={"content-type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error talking to Gemini: {exc}") from exc

    @staticmethod
    def _parse(data: dict) -> ModelReply:
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no_candidates")
            log.warning("empty reply from gemini: %s", reason)
            return ModelReply(finish_reason=str(reason))

        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []

        text_chunks: list[str] = []
        tool_calls: list[dict] = []
        text_signature: str | None = None

        for index, part in enumerate(parts):
            signature = part.get("thoughtSignature")
            if "text" in part:
                text_chunks.append(part["text"])
                if signature and text_signature is None:
                    text_signature = signature
            call = part.get("functionCall")
            if call:
                tool_call = {
                    # Gemini does not hand back a call id, so one is
                    # synthesised; it only has to be unique within a turn.
                    "id": f"call_{index}",
                    "name": call.get("name", ""),
                    "arguments": call.get("args") or {},
                }
                if signature:
                    tool_call["signature"] = signature
                tool_calls.append(tool_call)

        if not text_chunks and not tool_calls:
            log.warning("gemini returned no text and no tool calls (finish_reason=%s)", finish_reason)

        return ModelReply(
            text="".join(text_chunks).strip(),
            tool_calls=tool_calls,
            signature=text_signature,
            finish_reason=finish_reason,
        )

    # -- media ------------------------------------------------------------

    def _media_model(self) -> str:
        """Which model reads a voice note or a photo.

        Its own setting, because the cheapest model that is good enough to run
        a whole conversation is not necessarily the one you want reading
        Egyptian Arabic off a noisy voice note -- and vice versa. Falls back to
        the conversation model, so nothing has to be configured for this to
        work.
        """
        if settings.llm_media_model:
            return settings.llm_media_model.replace("models/", "")
        if self.auto_resolve and not self._resolved:
            self.resolve_model()
        return self.model

    def _generate_media(self, payload: dict, model: str) -> dict:
        """One non-conversational call. Shares the error mapping, not the loop."""
        url = f"{BASE_URL}/{API_VERSION}/models/{model}:generateContent"
        try:
            response = httpx.post(
                url,
                params={"key": self.api_key},
                json=payload,
                timeout=self.media_timeout,
                headers={"content-type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error talking to Gemini: {exc}") from exc

        if response.status_code == 429:
            raise ProviderError(f"rate limited on model {model!r}", kind="rate_limit")
        if response.status_code in (401, 403):
            raise ProviderError(
                f"auth rejected ({response.status_code}) for key {mask_key(self.api_key)}",
                kind="auth",
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"gemini media error {response.status_code} on {model!r}: {response.text[:300]}"
            )
        return response.json()

    @staticmethod
    def _first_text(data: dict) -> str:
        for candidate in data.get("candidates") or []:
            parts = (candidate.get("content") or {}).get("parts") or []
            joined = "".join(part.get("text", "") for part in parts).strip()
            if joined:
                return joined
        return ""

    def transcribe(self, audio: bytes, mime_type: str, *, hint: str = "") -> str:
        """A voice note, written down.

        Asked for a verbatim transcript and nothing else -- no summary, no
        translation, no answer to whatever was said. Egyptian Arabic stays in
        Arabic script and an English product name stays in Latin, because that
        is the mixture the agent's own prompt is written for and the mixture
        `search_terms` expects.
        """
        instruction = (
            "اكتب اللي اتقال في التسجيل ده حرفياً، بنفس اللهجة وبنفس الكلمات.\n"
            "- لو الكلام عربي اكتبه عربي، ولو فيه اسم منتج أو مقاس بالإنجليزي سيبه بالإنجليزي.\n"
            "- متلخصش، متترجمش، ومتردش على الكلام — انت بتفرّغ صوت وبس.\n"
            "- لو مفيش كلام مفهوم خالص، رد بكلمة واحدة: (غير مفهوم)"
        )
        if hint:
            instruction = f"{instruction}\n- سياق المحادثة: {hint}"

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": instruction},
                        {
                            "inline_data": {
                                "mime_type": mime_type or "audio/ogg",
                                "data": base64.b64encode(audio).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            # Deterministic: a transcript is not a place for creativity.
            "generationConfig": {"temperature": 0.0},
        }
        text = self._first_text(self._generate_media(payload, self._media_model()))
        if not text or text.strip() in {"(غير مفهوم)", "()"}:
            return ""
        return text

    #: The vision pass answers in this shape or not at all. A schema rather
    #: than a "reply with JSON" instruction, because the caller branches on
    #: `product_id` and a prose answer there is an outage, not a bad answer.
    _IMAGE_SCHEMA = {
        "type": "object",
        "properties": {
            "product_id": {"type": "string"},
            "confidence": {"type": "number"},
            "description": {"type": "string"},
            "is_garment": {"type": "boolean"},
        },
        "required": ["product_id", "confidence", "description", "is_garment"],
    }

    def inspect_image(self, image: bytes, mime_type: str, *, catalog: list[dict]) -> ImageReading:
        listing = "\n".join(
            f"- {item.get('product_id')}: {item.get('name')} "
            f"({item.get('category')}; ألوان: {', '.join(item.get('colors') or []) or '—'})"
            for item in catalog
        )
        instruction = (
            "دي صورة بعتها زبون لمحل هدوم. شوف الصورة وقارنها بالمنتجات اللي في اللستة دي بس.\n\n"
            f"{listing}\n\n"
            "قواعد لازم تلتزم بيها:\n"
            "- product_id لازم يكون واحد بالظبط من اللستة فوق، أو سلسلة فاضية لو مفيش حاجة قريبة.\n"
            "- متخترعش منتج مش في اللستة ومتقولش سعر ولا مقاس ولا إنه متوفر.\n"
            "- confidence رقم من 0 لـ 1 يعبر عن مدى تأكدك من المطابقة.\n"
            "- description: وصف قصير جداً للقطعة اللي في الصورة (نوعها ولونها) بالعامية المصرية.\n"
            "- is_garment: false لو الصورة مش قطعة هدوم أصلاً (إيصال، سكرين شوت، شخص، طرد، حاجة تانية)."
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": instruction},
                        {
                            "inline_data": {
                                "mime_type": mime_type or "image/jpeg",
                                "data": base64.b64encode(image).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "responseSchema": self._IMAGE_SCHEMA,
            },
        }
        raw = self._first_text(self._generate_media(payload, self._media_model()))
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"vision reply was not JSON: {raw[:200]}") from exc

        allowed = {item.get("product_id") for item in catalog}
        product_id = (parsed.get("product_id") or "").strip() or None
        if product_id not in allowed:
            # The one guarantee worth enforcing on this side: a product_id the
            # shop does not have is worse than no match at all.
            if product_id:
                log.warning("vision returned an unknown product_id %r; treating as no match", product_id)
            product_id = None

        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        return ImageReading(
            product_id=product_id,
            confidence=max(0.0, min(1.0, confidence)),
            description=str(parsed.get("description") or "").strip(),
            is_garment=bool(parsed.get("is_garment", True)),
        )

    # -- comments: classification (cheap, no tools, no history) -----------

    _COMMENT_SCHEMA = {
        "type": "object",
        "properties": {"category": {"type": "string", "enum": list(COMMENT_CATEGORIES)}},
        "required": ["category"],
    }

    def classify_comment(self, text: str) -> CommentClassification:
        # The same six buckets, described the same way as the OpenRouter
        # provider's `_COMMENT_INSTRUCTION`: `complaint` and `negative` are
        # told apart by who is writing, not by tone.
        instruction = (
            "دي كومنت وصل على بوست أو ريل لمحل هدوم على انستجرام. صنّفه لحاجة واحدة بس من دول:\n"
            '- "important": بيسأل سؤال فعلي عن المنتج، السعر، المقاس، التوفر، أو أوردر.\n'
            '- "positive": إعجاب أو تعليق إيجابي (قلوب، إيموچي حلوة، مدح) من غير سؤال فعلي.\n'
            '- "complaint": زبون فعلاً اشترى وعنده مشكلة حقيقية -- الأوردر اتأخر، وصله مقاس '
            "أو لون غلط، حاجة مكسورة، أو حد سأل ومحدش رد عليه.\n"
            '- "negative": تريقة أو رأي وحش من حد مش زبون -- شتيمة، أو تعليق سلبي على البراند '
            "من غير مشكلة في أوردر بعينه.\n"
            '- "spam": بوتات متابعين، لينكات نصب، كريبتو، إعلان لحاجة تانية خالص.\n'
            '- "neither": حاجة تانية -- بس بيمنشن صاحبه («@صاحبته شوفي دي») من غير '
            "سؤال حقيقي من الكاتب نفسه.\n\n"
            f"الكومنت:\n{text}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": instruction}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "responseSchema": self._COMMENT_SCHEMA,
            },
        }
        # The classifier's own model when one is configured, for the same
        # reason the OpenRouter provider takes one: comments are a live
        # public surface and must not move whenever the chat model does.
        model = settings.comment_classifier_model.replace("models/", "") or self._media_model()
        raw = self._first_text(self._generate_media(payload, model))
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"classification reply was not JSON: {raw[:200]}") from exc

        category = str(parsed.get("category") or "").strip().lower()
        if category not in COMMENT_CATEGORIES:
            log.warning("classify_comment returned an unknown category %r; treating as neither", category)
            category = "neither"
        return CommentClassification(category=category)


def _debug_dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _main() -> int:  # pragma: no cover - operational helper
    """python -m assistant.providers.gemini — what can this key actually call?"""
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        provider = GeminiProvider(auto_resolve=False)
    except ProviderError as exc:
        print(f"cannot start: {exc}")
        return 1

    print(f"key {mask_key(provider.api_key)}, configured model {provider.model or '(unset)'}\n")
    try:
        models = provider.available_models()
    except ProviderError as exc:
        print(f"listing failed: {exc}")
        return 1

    for name in models:
        marker = " <- configured" if name == provider.model else ""
        print(f"  {name}{marker}")
    print(f"\n{len(models)} models support generateContent.")
    print(f"would use: {provider.resolve_model(force=not provider.model)!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
