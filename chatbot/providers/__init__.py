"""Provider selection -- one config value, one class.

`build_provider` is the only place a concrete provider is named. Nothing above
this package imports a vendor module.
"""

from __future__ import annotations

import logging

from chatbot.providers.base import LLMProvider, ModelReply, ProviderError
from config.settings import settings

log = logging.getLogger("wanas.provider")

_override: LLMProvider | None = None


def build_provider(name: str | None = None) -> LLMProvider:
    name = (name or settings.llm_provider or "fake").lower()

    if name in {"fake", "rehearsal"}:
        from chatbot.providers.fake import RehearsalProvider

        return RehearsalProvider()
    if name == "scripted":
        from chatbot.providers.fake import ScriptedProvider

        return ScriptedProvider()
    if name == "gemini":
        from chatbot.providers.gemini import GeminiProvider

        return GeminiProvider()
    if name == "openrouter":
        from chatbot.providers.openrouter import OpenRouterProvider

        return OpenRouterProvider()

    raise ProviderError(f"unknown LLM_PROVIDER {name!r}")


def get_provider() -> LLMProvider:
    """The active provider.

    Falls back to the rehearsal stand-in when the real one cannot be built --
    a missing key is a deployment problem, and the harness staying usable is
    worth more than an import-time crash.
    """
    if _override is not None:
        return _override
    try:
        return build_provider()
    except ProviderError as exc:
        log.error("provider unavailable (%s); falling back to rehearsal mode", exc)
        from chatbot.providers.fake import RehearsalProvider

        return RehearsalProvider()


def set_provider(provider: LLMProvider | None) -> None:
    """Used by the tests and the harness to pin a provider."""
    global _override
    _override = provider


__all__ = ["LLMProvider", "ModelReply", "ProviderError", "build_provider", "get_provider", "set_provider"]
