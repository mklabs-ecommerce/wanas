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
* **Reasoning blocks ride in `ModelReply.signature`.** The conversation model
  is a reasoning model, and OpenRouter is explicit that for multi-turn tool
  calling the `reasoning_details` blocks an assistant turn came back with have
  to be sent back with it: they are the model's own working, and the request
  that follows a tool result is the model resuming that working rather than
  starting it again. Dropped -- which is what this file used to do -- the
  model gets its tool results back with no record of why it asked for them,
  and answers the question it reconstructs rather than the one that was asked.
  That is a whole class of "the reply has nothing to do with the message" on
  its own.

  So `signature` carries the `reasoning_details` list, which is exactly the
  job that field was added for: an opaque per-turn blob some providers require
  back, carried through history untouched and never inspected above this
  layer. It survives the database round trip because history is a JSON column.
  A signature arriving from *another* provider's stored session is a string,
  not a list, and is still dropped on the way out -- it means nothing here.

**One key, one vendor, one call shape -- but not always one model.** Chat,
voice-note transcription and photo reading all run through the *same*
``chat/completions`` call, keyed by ``OPENROUTER_API_KEY`` alone -- no second
key, no second provider, no vendor SDK. What ``LLM_MEDIA_MODEL`` adds is a
*model id* for the media calls, because the cheapest model good enough to run
a whole conversation is not necessarily the one you want reading Egyptian
Arabic off a voice note, and vice versa. ``gemini.py`` has had this setting
since it was written (``_media_model``); this file simply ignored it, which
is how a chat model with no audio endpoint at all came to be asked to
transcribe. The two therefore have *separate* defaults -- ``DEFAULT_MODEL``
for the tool loop, ``DEFAULT_MEDIA_MODEL`` for anything with sound or pixels
in it -- so a deployment that sets neither still gets a model that can hear.

* ``transcribe()`` (voice notes) sends an ``input_audio`` content part (the
  OpenAI-compatible shape: base64 payload plus a short format string) with an
  instruction asking for a verbatim transcript; the reply text *is* the
  transcript.
* ``inspect_image()`` (photos) sends an ``image_url`` content part carrying a
  base64 data URI.

``supports_audio`` / ``supports_vision`` are unconditionally True, declared at
construction per the "decide before spending a call" contract every provider
follows: both media paths need nothing beyond the OpenRouter key the provider
already refused to construct without. They stay declared rather than
discovered even now -- what they promise is that *this provider* can reach a
media call, not that whichever model id is configured has an audio endpoint.
A model that has none answers 404 and `media.py` falls back to a person, which
is the same handoff it would have done anyway.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

from assistant.messages import ASSISTANT, TOOL_RESULTS, USER
from assistant.providers.base import (
    COMMENT_CATEGORIES,
    LEGACY_COMMENT_CATEGORIES,
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

#: The conversation model -- the tool loop and every sentence a customer
#: reads -- unless LLM_MODEL pins another name. Chosen over the media model
#: below on the two things that cost this shop money: it does not invent
#: catalogue facts (it refuses and escalates instead of naming colours or
#: measurements nobody looked up), and it acts on the results it is handed.
DEFAULT_MODEL = "z-ai/glm-5.3-flash"

#: The model that reads a voice note or a photo, unless LLM_MEDIA_MODEL pins
#: another name. It is a *separate* default rather than "whatever chat runs
#: on" because the conversation model above has no audio endpoint at all:
#: OpenRouter answers "no endpoints found that support input audio", and a
#: deployment that configured nothing would silently turn every voice note
#: into a person's job. That is the bug LLM_MEDIA_MODEL was wired up to fix,
#: and defaulting media back to chat would reintroduce it for anyone who
#: never set the variable.
DEFAULT_MEDIA_MODEL = "google/gemini-3.1-flash-lite"

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


#: Finish reasons meaning the model stopped at its token ceiling rather than
#: at the end of what it had to say. The same set `assistant/agent.py` guards
#: a chat reply with, kept here too because a *transcript* that stops early is
#: the more dangerous of the two: a cut-off reply looks wrong, a cut-off
#: transcript looks like a shorter message.
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


class OpenRouterProvider(LLMProvider):
    name = "openrouter"

    #: How long one chat call may take. Six minutes, not the thirty seconds
    #: this used to be, because the conversation model's *tail* is what the
    #: customer feels: `z-ai/glm-5.3-flash` has a median around six seconds
    #: but was measured at 115s and 125s on two of eight single-hop calls
    #: against this shop's own prompt and tools. At thirty seconds those two
    #: became `ReadTimeout` -> ProviderError -> the generic apology, so a
    #: quarter of turns failed outright rather than answering slowly.
    #:
    #: Affordable because nobody is holding the connection open waiting for
    #: it: the webhook claims the message, records it and returns 200, and the
    #: turn runs afterwards on a dispatcher worker thread
    #: (`assistant/dispatcher.py`). Meta's "reply quickly" expectation is
    #: satisfied by the endpoint, not by the model.
    #:
    #: What this does *not* bound is a whole turn: `tool_loop_cap` (8) chat
    #: calls at six minutes each is a long time to hold the one database
    #: session `runtime.handle_message` opens around the turn. Eight workers
    #: against a pool of 5+10 leaves room, but a turn that slow is pathology
    #: rather than latency, and the ceiling to lower is the loop cap.
    DEFAULT_TIMEOUT = 360.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        if not self.api_key:
            raise ProviderError("OPENROUTER_API_KEY is not set", kind="auth")
        self.model = (model or settings.llm_model or "").strip() or DEFAULT_MODEL
        self.timeout = self.DEFAULT_TIMEOUT if timeout is None else timeout
        #: Media is read outside the customer's turn (the reply is already on
        #: its way to them), so it can afford to wait longer than a chat call
        #: someone is sitting in front of -- same reasoning gemini.py's
        #: media_timeout follows. Now that chat itself waits six minutes this
        #: is a floor rather than a raise, and it stays so that pinning a
        #: short chat timeout cannot quietly shorten a transcription too.
        self.media_timeout = max(self.timeout, 60.0)

        # Both declared, not discovered (base.py): the runtime decides between
        # reading a voice note / photo and handing it to a person *before*
        # spending a call. Nothing gates either one -- they run on the same
        # key and endpoint as chat, which the constructor has just required to
        # exist. Not necessarily the same *model*: see `_media_model`.
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
                # The reasoning blocks this turn was produced with, handed
                # straight back so the model can resume its own working
                # instead of reconstructing it -- see the module docstring.
                # Only ever a list: a `signature` shaped like anything else
                # came from another provider's protocol and means nothing
                # here, so it is dropped exactly as it always was.
                signature = message.get("signature")
                if isinstance(signature, list) and signature:
                    entry["reasoning_details"] = signature
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

    #: The completion ceiling for one chat call.
    #:
    #: 4096, not the 1024 it was, because on a reasoning model that budget is
    #: not the reply -- it is the reasoning *plus* the reply. `z-ai/glm-5.3-flash`
    #: was measured spending 40 to 533 reasoning tokens on ordinary shop
    #: questions and, on one in six, all 1024 of them: `finish_reason=length`,
    #: no content, no tool calls. `agent.run_turn` sees an empty reply and the
    #: customer gets the generic apology, which is exactly what happened in
    #: production to "الشحن كام وبيوصل امتى؟".
    #:
    #: Raising it also made the tail *shorter*, not longer -- median 10.4s
    #: against 10.9s, worst case 15.3s against 42.5s -- because the slowest
    #: calls were the ones grinding into the ceiling and returning nothing.
    #: There is no cheaper lever: this endpoint answers
    #: "Reasoning is mandatory for this endpoint and cannot be disabled."
    #: to `reasoning: {"enabled": false}`.
    #: Raised again from 4096 for the same reason it went from 1024 to 4096,
    #: one failure mode further on. At 4096 the budget was enough that the
    #: model rarely returned *nothing*, but "rarely nothing" is not the same
    #: as "always finished": a long reply (an order summary carries the name,
    #: the address, the phone, the line items, shipping and a total) behind
    #: several hundred reasoning tokens can still run out mid-sentence, and
    #: `finish_reason="length"` is a reply cut off wherever the counter
    #: stopped -- half a word, a dangling question. That is one of the shapes
    #: the garbled replies took in production.
    #:
    #: Costed in generated tokens, so a ceiling nothing reaches is free; the
    #: measured ceiling on ordinary shop turns is under 1000. This is
    #: headroom, and `agent.run_turn`'s truncation guard is the guarantee
    #: underneath it -- a ceiling can always be reached by something, and a
    #: half-written sentence must never be sent whatever the budget was.
    CHAT_MAX_TOKENS = 8192

    def _routing(self) -> dict | None:
        """Which upstreams OpenRouter may serve this model from.

        **A model id is not a serving stack.** OpenRouter routes one id across
        every provider that hosts it -- twenty-three of them for
        `z-ai/glm-5.3-flash`, at fp8 and at quantizations that decline to say
        what they are -- and load-balances between them per request. Six
        identical requests from this codebase were measured landing on three
        different providers.

        That would be an availability feature and nothing more, except for
        what the default does with parameters: with `require_parameters` off,
        a provider that does not implement `temperature` is still sent the
        request and **silently drops it**. The reply is then sampled at that
        stack's own default, which is typically 1.0 rather than the 0.3 below.
        Egyptian Arabic is the first thing that degrades under that, and it
        degrades *per request* -- most replies fine, an occasional one
        grammatically broken or answering a question nobody asked. That is the
        shape of the garbled replies this shop saw in production, and no
        amount of prompt work reaches it, because the prompt is not what
        changed between a good reply and a bad one.

        So the candidate set is filtered rather than left open:

        * `require_parameters` -- only providers that actually implement
          everything in the payload. This is the correctness half: it is what
          makes `temperature` a setting rather than a suggestion.
        * `quantizations` -- fp8 and up, never "unknown".
        * `order` -- preference within what survives the filter, first-party
          first.
        * `allow_fallbacks` stays **on**. The point is to exclude stacks that
          answer badly, not to make one provider a single point of failure;
          anything the filter admits can answer if the preferred ones cannot.

        Returns None when nothing is configured, which leaves OpenRouter's
        default behaviour exactly as it was -- the tests that pin the payload
        shape for a bare provider keep passing, and a deployment can opt out.
        """
        routing: dict = {}
        if settings.openrouter_providers:
            routing["order"] = list(settings.openrouter_providers)
        if settings.openrouter_quantizations:
            routing["quantizations"] = list(settings.openrouter_quantizations)
        if not routing:
            return None
        routing["allow_fallbacks"] = True
        routing["require_parameters"] = True
        return routing

    def _build_payload(self, system_prompt: str, history: list[dict], tools: list) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": self._messages(system_prompt, history),
            "temperature": 0.3,
            "max_tokens": self.CHAT_MAX_TOKENS,
        }
        routing = self._routing()
        if routing:
            payload["provider"] = routing
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

        # Carried, never read. `_messages` sends it back on the next hop of
        # the same tool loop, which is what OpenRouter asks for and what keeps
        # a post-tool-result reply attached to the question that caused it.
        reasoning = message.get("reasoning_details")
        signature = reasoning if isinstance(reasoning, list) and reasoning else None

        return ModelReply(
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            signature=signature,
        )

    def _media_model(self) -> str:
        """Which model reads a voice note or a photo.

        Same setting `gemini.py::_media_model` reads. The fallback is
        `DEFAULT_MEDIA_MODEL` rather than the conversation model, because the
        conversation model cannot hear -- see the constant. A caller that
        pins `LLM_MODEL` to something that *can* read media and leaves
        `LLM_MEDIA_MODEL` blank gets this default instead of their own model,
        which is the one cost of the arrangement and is worth it: the
        alternative silently breaks voice notes for everyone who configures
        nothing. It is a model id only -- the key, the transport and the
        error mapping are the ones chat already uses.
        """
        return (settings.llm_media_model or "").strip() or DEFAULT_MEDIA_MODEL

    # -- media: voice notes (media model, input_audio part) -----------------

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

        model = self._media_model()
        payload = {
            "model": model,
            # Deterministic: a transcript is not a place for creativity.
            "temperature": 0.0,
            # Room for a long voice note. Arabic runs two to three tokens a
            # word here, and a customer describing an order and an address in
            # one recording overran 1024 -- which, before the guard above,
            # arrived as a shorter message rather than as a failure.
            "max_tokens": 4096,
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
                f"rate limited on model {model!r}: {response.text[:300]}",
                kind="rate_limit",
            )
        if response.status_code in (401, 403):
            raise ProviderError(
                f"auth rejected ({response.status_code}) for key {mask_key(self.api_key)}: "
                f"{response.text[:300]}",
                kind="auth",
            )
        if response.status_code >= 400:
            # A model with no audio endpoint answers 404 here, which used to
            # read as a generic provider error -- so "every voice note became
            # a handoff" looked like flakiness rather than the one-line
            # configuration mistake it is. Name it.
            kind = (
                "unsupported"
                if response.status_code == 404 and "input audio" in response.text.lower()
                else "provider_error"
            )
            raise ProviderError(
                f"openrouter transcription error {response.status_code} on model {model!r}: "
                f"{response.text[:500]}",
                kind=kind,
            )

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            return ""

        # A transcript that ran into the ceiling is half of what the customer
        # said, and it is worse than nothing: it does not read as broken, it
        # reads as a *shorter message*, and the whole turn is then built on
        # it. "عايز الهودي الأسود لارج بس لو مش متاح خليه ميديم" cut after
        # "الأسود" is a different order. Everything downstream trusts this
        # string as the customer's own words -- `search_terms`, the model, the
        # cart -- so there is nowhere further down to catch it.
        #
        # Empty is the documented "hand this to a person" signal
        # (`assistant/media.py::transcribe_voice`), and a person listening to
        # the voice note is exactly right here: the words exist, we just could
        # not write all of them down.
        finish_reason = choices[0].get("finish_reason")
        if str(finish_reason or "").strip().lower() in _TRUNCATED_FINISH_REASONS:
            log.warning(
                "transcript from model %r hit the completion ceiling (finish_reason=%s); "
                "handing the voice note to a person rather than answering half of it",
                model,
                finish_reason,
            )
            return ""

        raw = (choices[0].get("message") or {}).get("content")
        text = self._clean_transcript(str(raw or ""))
        if not text or text in {"(غير مفهوم)", "()"}:
            return ""
        return text

    # -- media: photos (vision, media model, same endpoint) -----------------

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

        model = self._media_model()
        payload = {
            "model": model,
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
                f"rate limited on vision model {model!r}: {response.text[:300]}",
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
                f"openrouter vision error {response.status_code} on model {model!r}: "
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
        # The dashboard's chart reading is a vision call like any other, so it
        # follows the media model too: leaving it on the conversation model
        # would mean two different models reading two different pictures.
        model = self._media_model()
        payload = {
            "model": model,
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
                f"rate limited on vision model {model!r}: {response.text[:300]}",
                kind="rate_limit",
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"openrouter size-chart error {response.status_code} on model "
                f"{model!r}: {response.text[:500]}"
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

    #: Six categories, and the two new ones are described by *who is writing*
    #: rather than by tone: a complaint and a hater's insult read alike to a
    #: sentiment model, and they get opposite treatment here.
    _COMMENT_INSTRUCTION = (
        "دي كومنت وصل على بوست أو ريل لمحل هدوم ستريت وير على انستجرام. "
        "صنّفه لحاجة واحدة بس من دول:\n"
        '- "price": بيسأل عن سعر قطعة («بكام؟»، «السعر كام؟»).\n'
        '- "availability": بيسأل لسه موجود ولا خلص، أو متوفر ولا لأ.\n'
        '- "size": سؤال عن المقاسات، أو عن مقاسه هو، أو عن جدول المقاسات.\n'
        '- "variant": بيسأل عن لون تاني أو موديل تاني أو حاجة شبهها.\n'
        '- "product_info": بيسأل عن الخامة أو القماش أو الجودة أو الغسيل.\n'
        '- "order_status": زبون اشترى بالفعل وبيسأل أوردره فين أو هيوصل امتى.\n'
        '- "complaint": زبون اشترى وعنده مشكلة حقيقية -- اتأخر، وصله مقاس أو لون '
        "غلط، حاجة مكسورة، أو سأل ومحدش رد عليه.\n"
        '- "positive": مدح أو إعجاب من غير سؤال («جميل»، «تحفة»، قلوب).\n'
        '- "negative": تريقة أو رأي وحش من حد مش زبون، من غير مشكلة في أوردر بعينه.\n'
        '- "tag_friend": بيمنشن صاحبه بس («@سارة بصي دي») من غير سؤال منه هو.\n'
        '- "spam": بوتات متابعين، لينكات نصب، كريبتو، إعلان لحاجة تانية خالص.\n'
        '- "other": أي حاجة تانية فيها كلام حقيقي مش داخلة في اللي فوق.\n\n'
        "لو الكومنت فيه أكتر من حاجة، اختار اللي الزبون عايزه أكتر.\n\n"
        "الكومنت:\n{comment}\n\n"
        "رد بـ JSON بس، بالشكل ده بالظبط: "
        '{{"category": "price|availability|size|variant|product_info|order_status'
        '|complaint|positive|negative|tag_friend|spam|other"}}'
    )

    def classify_comment(self, text: str) -> CommentClassification:
        instruction = self._COMMENT_INSTRUCTION.format(comment=text)
        payload = {
            # Its own model when one is configured. Not for cost -- this call
            # is ~250 tokens in and rounds to nothing -- but for decoupling:
            # without it, upgrading the chat model silently changes what
            # happens on a live public surface, and one model being pulled or
            # rate-limited takes chat and comments down together. Blank
            # reuses the chat model, which is the behaviour this replaced.
            "model": settings.comment_classifier_model or self.model,
            "temperature": 0.0,
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": instruction}],
        }

        # Named in the errors below rather than `self.model`: a classifier
        # outage has to say which model was actually called.
        model = payload["model"]
        response = self._post(payload)
        if response.status_code == 429:
            raise ProviderError(
                f"rate limited on model {model!r}: {response.text[:300]}",
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
                f"{model!r}: {response.text[:500]}"
            )

        body = response.json()
        choices = body.get("choices") or []
        raw = (choices[0].get("message") or {}).get("content") if choices else None
        try:
            parsed = json.loads(str(raw or "").strip() or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"classification reply was not JSON: {raw!r:.200}") from exc

        category = str(parsed.get("category") or "").strip().lower()
        category = LEGACY_COMMENT_CATEGORIES.get(category, category)
        if category not in COMMENT_CATEGORIES:
            log.warning("classify_comment returned an unknown category %r; treating as other", category)
            category = "other"
        return CommentClassification(category=category)


def _debug_dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
