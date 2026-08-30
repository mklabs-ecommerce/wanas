"""OpenRouter provider -- the default conversation model.

Talks to OpenRouter's OpenAI-compatible ``chat/completions`` endpoint with
httpx rather than any vendor SDK, the same raw-HTTPS discipline gemini.py
follows: one less dependency, and every vendor detail stays inside this file
where the boundary says it belongs.

Quirks absorbed here, none of which leak upward:

* Tool calls come back as ``tool_calls[].function.arguments`` -- a JSON
  *string*, not an object -- and travel out the same way. Translated in both
  directions; a `ModelReply` always carries parsed dicts.
* Tool results are their own ``role: "tool"`` messages keyed by
  ``tool_call_id``, not parts bundled into a user turn.
* An assistant turn that carries tool calls is sent with ``content: null``
  rather than an empty string, which strict routers prefer.
* **No thought signatures.** Nothing in this protocol demands an opaque blob
  back, so `ModelReply.signature` stays None and any signature arriving in
  history from another provider's stored session is dropped on the way out --
  it means nothing here.

**One model for everything.** Chat, voice-note transcription and photo
reading all run on the *same* model through the *same*
``chat/completions`` call, keyed by ``OPENROUTER_API_KEY`` alone -- no second
key and no separate media model anywhere in this file.

* ``transcribe()`` (voice notes) sends an ``input_audio`` content part (the
  OpenAI-compatible shape: base64 payload plus a short format string) with an
  instruction asking for a verbatim transcript; the reply text *is* the
  transcript.
* ``inspect_image()`` (photos) sends an ``image_url`` content part carrying a
  base64 data URI.

``supports_audio`` / ``supports_vision`` are unconditionally True, declared at
construction per the "decide before spending a call" contract every provider
follows: both media paths need nothing beyond the OpenRouter key the provider
already refused to construct without.
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
    SizeChartReading,
    normalise_chart_reading,
)
from assistant.providers.gemini import mask_key
from config.settings import settings

log = logging.getLogger("wanas.provider.openrouter")

BASE_URL = "https://openrouter.ai/api/v1"

#: The model everything runs on -- conversation, voice-note transcription and
#: photo reading alike -- unless LLM_MODEL pins another name.
DEFAULT_MODEL = "google/gemini-3.1-flash-lite"

#: Mime type -> the short format string the ``input_audio`` content part
#: wants. Falls back to "ogg" (what WhatsApp voice notes are) for anything
#: unrecognised rather than refusing outright; a wrong-but-plausible format is
#: still worth letting the model have a look at.
_AUDIO_FORMATS = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


class OpenRouterProvider(LLMProvider):
    name = "openrouter"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ):
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        if not self.api_key:
            raise ProviderError("OPENROUTER_API_KEY is not set", kind="auth")
        self.model = (model or settings.llm_model or "").strip() or DEFAULT_MODEL
        self.timeout = timeout
        #: Media is read outside the customer's turn (the reply is already on
        #: its way to them), so it can afford to wait longer than a chat call
        #: someone is sitting in front of -- same reasoning gemini.py's
        #: media_timeout follows.
        self.media_timeout = max(timeout, 60.0)

        # Both declared, not discovered (base.py): the runtime decides between
        # reading a voice note / photo and handing it to a person *before*
        # spending a call. Nothing gates either one -- they run on the same
        # key and model as chat, which the constructor has just required to
        # exist.
        self.supports_audio = True
        self.supports_vision = True

    # -- translation ------------------------------------------------------

    @staticmethod
    def _schema(spec) -> dict:
        declaration: dict = {
            "type": "function",
            "function": {"name": spec.name, "description": spec.description},
        }
        if spec.properties:
            # The neutral property dicts already are plain JSON Schema, which
            # is exactly what this API speaks -- passed through untouched,
            # unlike Gemini's uppercase dialect.
            declaration["function"]["parameters"] = {
                "type": "object",
                "properties": spec.properties,
                "required": list(spec.required),
            }
        # A function with no arguments omits `parameters` entirely, matching
        # what the Gemini side already learned: an empty object invites the
        # model to invent arguments.
        return declaration

    @staticmethod
    def _messages(system_prompt: str, history: list[dict]) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for message in history:
            role = message.get("role")
            if role == USER:
                messages.append({"role": "user", "content": message.get("content", "")})
            elif role == ASSISTANT:
                entry: dict = {"role": "assistant", "content": message.get("content") or ""}
                calls = []
                for call in message.get("tool_calls") or []:
                    calls.append(
                        {
                            "id": call.get("id") or call.get("name", ""),
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                            },
                        }
                    )
                if calls:
                    entry["tool_calls"] = calls
                    if not entry["content"]:
                        entry["content"] = None
                # A `signature` on this turn (or on any of its calls) belongs
                # to another provider's protocol; dropped, never inspected.
                messages.append(entry)
            elif role == TOOL_RESULTS:
                for result in message.get("results") or []:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.get("id"),
                            "content": json.dumps(result.get("content"), ensure_ascii=False),
                        }
                    )
        return messages

    def _build_payload(self, system_prompt: str, history: list[dict], tools: list) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": self._messages(system_prompt, history),
            "temperature": 0.3,
            "max_tokens": 1024,
        }
        if tools:
            payload["tools"] = [self._schema(spec) for spec in tools]
        return payload

    # -- request ----------------------------------------------------------

    def generate(self, system_prompt: str, history: list[dict], tools: list) -> ModelReply:
        response = self._post(self._build_payload(system_prompt, history, tools))

        if response.status_code == 429:
            raise ProviderError(
                f"rate limited on model {self.model!r}: {response.text[:300]}",
                kind="rate_limit",
            )
        if response.status_code in (401, 403):
            raise ProviderError(
                f"auth rejected ({response.status_code}) for key {mask_key(self.api_key)}: "
                f"{response.text[:300]}",
                kind="auth",
            )
        if response.status_code >= 400:
            # The body is kept in the message; LLM_DEBUG_PAYLOAD has already
            # logged exactly what was sent.
            raise ProviderError(
                f"openrouter error {response.status_code} on model {self.model!r}: {response.text[:500]}"
            )

        return self._parse(response.json())

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            # Optional attribution OpenRouter asks for; never required.
            "X-Title": "Wanas Gallery",
        }
        if settings.public_base_url:
            headers["HTTP-Referer"] = settings.public_base_url
        return headers

    def _post(self, payload: dict, *, timeout: float | None = None) -> httpx.Response:
        url = f"{BASE_URL}/chat/completions"

        if settings.llm_debug_payload:
            # Off by default. The API key travels in the Authorization header,
            # never in the body, so the dumped payload carries no credential.
            log.warning("POST %s payload:\n%s", url, _debug_dump(payload))

        try:
            # Media calls may wait longer than chat (nobody is sitting in
            # front of them); everything else rides the constructor timeout.
            return httpx.post(
                url,
                json=payload,
                timeout=timeout if timeout is not None else self.timeout,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error talking to OpenRouter: {exc}") from exc

    @staticmethod
    def _parse(data: dict) -> ModelReply:
        choices = data.get("choices") or []
        if not choices:
            reason = (data.get("error") or {}).get("message") or "no_candidates"
            log.warning("empty reply from openrouter: %s", reason)
            return ModelReply(finish_reason=str(reason))

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")

        tool_calls: list[dict] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            raw_arguments = function.get("arguments")
            try:
                # Arguments arrive as a JSON string; garbage here must not
                # reach the tools dressed up as empty arguments, where it
                # would read as a refusal the model never made.
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments or {})
                )
            except (json.JSONDecodeError, TypeError) as exc:
                raise ProviderError(
                    f"tool call {call.get('id') or index} had unparseable arguments: {raw_arguments!r}"
                ) from exc
            tool_calls.append(
                {
                    "id": call.get("id") or f"call_{index}",
                    "name": function.get("name", ""),
                    "arguments": arguments or {},
                }
            )

        text = str(message.get("content") or "").strip()
        if not text and not tool_calls:
            log.warning("openrouter returned no text and no tool calls (finish_reason=%s)", finish_reason)

        return ModelReply(text=text, tool_calls=tool_calls, finish_reason=finish_reason)

    # -- media: voice notes (same model, input_audio part) ------------------

    #: Asked for a verbatim transcript and nothing else -- no summary, no
    #: translation, no answer to whatever was said. Egyptian Arabic stays in
    #: Arabic script and an English product name stays in Latin, because that
    #: is the mixture the agent's own prompt is written for and the mixture
    #: `search_terms` expects. Same instruction gemini.py's transcribe uses.
    _TRANSCRIBE_INSTRUCTION = (
        "اكتب اللي اتقال في التسجيل ده حرفياً، بنفس اللهجة وبنفس الكلمات.\n"
        "- لو الكلام عربي اكتبه عربي، ولو فيه اسم منتج أو مقاس بالإنجليزي سيبه بالإنجليزي.\n"
        "- متلخصش، متترجمش، ومتردش على الكلام — انت بتفرّغ صوت وبس.\n"
        "- لو مفيش كلام مفهوم خالص، رد بكلمة واحدة: (غير مفهوم)"
    )

    @staticmethod
    def _clean_transcript(raw: str) -> str:
        """Bare words only. A chat model may dress a transcript in quotes or
        a code fence; that is presentation, not anything that was said."""
        text = raw.strip()
        if text.startswith("```"):
            text = text[3:]
            if text[:4].lower() == "text":
                text = text[4:]
            text = text.strip()
            if text.endswith("```"):
                text = text[:-3]
            return text.strip()
        while len(text) >= 2 and text[0] in "\"'\u00ab\u201c" and text[-1] in "\"'\u00bb\u201d":
            text = text[1:-1].strip()
        return text

    def transcribe(self, audio: bytes, mime_type: str, *, hint: str = "") -> str:
        """A voice note, written down -- the same chat/completions call chat
        uses, with the audio attached as an ``input_audio`` content part."""
        instruction = self._TRANSCRIBE_INSTRUCTION
        if hint:
            # Soft context for names it might otherwise mishear (a vocabulary/
            # spelling nudge), never an instruction to act on.
            instruction = f"{instruction}\n- سياق المحادثة: {hint}"

        payload = {
            "model": self.model,
            # Deterministic: a transcript is not a place for creativity.
            "temperature": 0.0,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(audio).decode("ascii"),
                                "format": _AUDIO_FORMATS.get((mime_type or "").lower(), "ogg"),
                            },
                        },
                    ],
                }
            ],
        }

        response = self._post(payload, timeout=self.media_timeout)
        if response.status_code == 429:
            raise ProviderError(
                f"rate limited on model {self.model!r}: {response.text[:300]}",
                kind="rate_limit",
            )
        if response.status_code in (401, 403):
            raise ProviderError(
                f"auth rejected ({response.status_code}) for key {mask_key(self.api_key)}: "
                f"{response.text[:300]}",
                kind="auth",
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"openrouter transcription error {response.status_code} on model {self.model!r}: "
                f"{response.text[:500]}"
            )

        body = response.json()
        choices = body.get("choices") or []
        raw = (choices[0].get("message") or {}).get("content") if choices else None
        text = self._clean_transcript(str(raw or ""))
        if not text or text in {"(غير مفهوم)", "()"}:
            return ""
        return text

    # -- media: photos (vision, same model, same endpoint) ------------------

    #: The vision pass answers in this shape or not at all. A schema rather
    #: than a "reply with JSON" instruction, because the caller branches on
    #: `product_id` and a prose answer there is an outage, not a bad answer.
    #: Same contract gemini.py's `_IMAGE_SCHEMA` enforces.
    _IMAGE_INSTRUCTION_TEMPLATE = (
        "دي صورة بعتها زبون لمحل هدوم. شوف الصورة وقارنها بالمنتجات اللي في اللستة دي بس.\n\n"
        "{listing}\n\n"
        "قواعد لازم تلتزم بيها:\n"
        "- product_id لازم يكون واحد بالظبط من اللستة فوق، أو سلسلة فاضية لو مفيش حاجة قريبة.\n"
        "- متخترعش منتج مش في اللستة ومتقولش سعر ولا مقاس ولا إنه متوفر.\n"
        "- confidence رقم من 0 لـ 1 يعبر عن مدى تأكدك من المطابقة.\n"
        "- description: وصف قصير جداً للقطعة اللي في الصورة (نوعها ولونها) بالعامية المصرية.\n"
        "- is_garment: false لو الصورة مش قطعة هدوم أصلاً (إيصال، سكرين شوت، شخص، طرد، حاجة تانية).\n\n"
        'رد بـ JSON بس، بالشكل ده بالظبط: '
        '{{"product_id": "...", "confidence": 0.0, "description": "...", "is_garment": true}}'
    )

    def inspect_image(self, image: bytes, mime_type: str, *, catalog: list[dict]) -> ImageReading:
        listing = "\n".join(
            f"- {item.get('product_id')}: {item.get('name')} "
            f"({item.get('category')}; ألوان: {', '.join(item.get('colors') or []) or '—'})"
            for item in catalog
        )
        instruction = self._IMAGE_INSTRUCTION_TEMPLATE.format(listing=listing)
        data_uri = f"data:{mime_type or 'image/jpeg'};base64,{base64.b64encode(image).decode('ascii')}"

        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }

        response = self._post(payload)
        if response.status_code == 429:
            raise ProviderError(
                f"rate limited on vision model {self.model!r}: {response.text[:300]}",
                kind="rate_limit",
            )
        if response.status_code in (401, 403):
            raise ProviderError(
                f"auth rejected ({response.status_code}) for key {mask_key(self.api_key)}: "
                f"{response.text[:300]}",
                kind="auth",
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"openrouter vision error {response.status_code} on model {self.model!r}: "
                f"{response.text[:500]}"
            )

        body = response.json()
        choices = body.get("choices") or []
        raw = (choices[0].get("message") or {}).get("content") if choices else None
        raw = str(raw or "").strip()
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

    # -- media: a size-chart picture, read as data --------------------------

    #: English, unlike the customer-facing prompts above, because what comes
    #: back is a schema a staff member edits -- not a sentence anyone is sent.
    _CHART_INSTRUCTION_TEMPLATE = (
        "This is a garment size chart. Read the numbers off it into JSON.\n\n"
        "The product is sold in these sizes: {sizes}\n\n"
        "Rules:\n"
        "- Only report a size that appears in the picture AND in that list. "
        "Omit any other row entirely; do not invent or interpolate one.\n"
        "- Every measurement value must be a number you can actually read. "
        "If a cell is blank or unreadable, leave that key out of that size.\n"
        "- measurements: one entry per column, in the order they appear, each "
        '{{"key": "snake_case_ascii", "label_en": "...", "label_ar": "..."}}. '
        "Translate the label to Arabic yourself if the chart is English-only, "
        "and to English if it is Arabic-only.\n"
        '- unit: "cm" or "in". Use "cm" if the chart does not say.\n'
        "- confidence: 0 to 1, how legible the chart was.\n"
        "- notes: one short sentence naming anything you could not resolve, "
        "or an empty string.\n\n"
        "Reply with JSON only, exactly this shape:\n"
        '{{"measurements": [{{"key": "width", "label_en": "Width", "label_ar": "العرض"}}], '
        '"sizes": {{"S": {{"width": 54}}}}, "unit": "cm", "confidence": 0.0, "notes": ""}}'
    )

    def read_size_chart(self, image: bytes, mime_type: str, *, sizes: list[str]) -> SizeChartReading:
        instruction = self._CHART_INSTRUCTION_TEMPLATE.format(
            sizes=", ".join(sizes) or "(unknown -- report whatever the chart shows)"
        )
        data_uri = f"data:{mime_type or 'image/png'};base64,{base64.b64encode(image).decode('ascii')}"
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 1500,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }

        response = self._post(payload)
        if response.status_code == 429:
            raise ProviderError(
                f"rate limited on vision model {self.model!r}: {response.text[:300]}",
                kind="rate_limit",
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"openrouter size-chart error {response.status_code} on model "
                f"{self.model!r}: {response.text[:500]}"
            )

        body = response.json()
        choices = body.get("choices") or []
        raw = str(((choices[0].get("message") or {}).get("content") if choices else "") or "").strip()
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"size-chart reply was not JSON: {raw[:200]}") from exc

        return normalise_chart_reading(parsed, sizes=sizes)

    # -- comments: classification (cheap, no tools, no history) -----------

    _COMMENT_INSTRUCTION = (
        "دي كومنت وصل على بوست أو ريل لمحل هدوم على انستجرام. صنّفه لحاجة واحدة بس من دول:\n"
        '- "important": بيسأل سؤال فعلي عن المنتج، السعر، المقاس، التوفر، أو أوردر.\n'
        '- "positive": إعجاب أو تعليق إيجابي (قلوب، إيموچي حلوة، مدح) من غير سؤال فعلي.\n'
        '- "negative": شكوى أو تعليق سلبي.\n'
        '- "neither": حاجة تانية -- سبام، أو بس بيمنشن صاحبه («@صاحبته شوفي دي») من غير '
        "سؤال حقيقي من الكاتب نفسه.\n\n"
        "الكومنت:\n{comment}\n\n"
        'رد بـ JSON بس، بالشكل ده بالظبط: {{"category": "important|positive|negative|neither"}}'
    )

    def classify_comment(self, text: str) -> CommentClassification:
        instruction = self._COMMENT_INSTRUCTION.format(comment=text)
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": instruction}],
        }

        response = self._post(payload)
        if response.status_code == 429:
            raise ProviderError(
                f"rate limited on model {self.model!r}: {response.text[:300]}",
                kind="rate_limit",
            )
        if response.status_code in (401, 403):
            raise ProviderError(
                f"auth rejected ({response.status_code}) for key {mask_key(self.api_key)}: "
                f"{response.text[:300]}",
                kind="auth",
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"openrouter classification error {response.status_code} on model "
                f"{self.model!r}: {response.text[:500]}"
            )

        body = response.json()
        choices = body.get("choices") or []
        raw = (choices[0].get("message") or {}).get("content") if choices else None
        try:
            parsed = json.loads(str(raw or "").strip() or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"classification reply was not JSON: {raw!r:.200}") from exc

        category = str(parsed.get("category") or "").strip().lower()
        if category not in COMMENT_CATEGORIES:
            log.warning("classify_comment returned an unknown category %r; treating as neither", category)
            category = "neither"
        return CommentClassification(category=category)


def _debug_dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
