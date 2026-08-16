"""The provider boundary.

Nothing above this layer may import a vendor SDK. Swapping providers means
writing one class and changing one config value -- that is a hard
architectural boundary, not a nice-to-have, because cost is the reason the
provider may change.

A provider knows nothing about orders, carts or products. It translates
between the neutral message format and one vendor's API, and absorbs the
vendor's quirks: how tool schemas are described, whether an empty parameter
object is accepted, opaque per-call signatures that must be echoed back, and
whether reasoning has to be switched off.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ProviderError(Exception):
    """Anything the provider could not recover from."""

    def __init__(self, message: str, *, kind: str = "provider_error"):
        super().__init__(message)
        #: `rate_limit`, `auth`, `provider_error`. The agent maps these to
        #: customer-facing behaviour: a rate limit is "try again in a minute",
        #: an auth failure is a deployment problem no customer message fixes.
        self.kind = kind


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    #: Opaque per-turn blob some providers attach to the assistant's own text
    #: and require back on the next request. Meaningless above this layer;
    #: carried, never inspected. Per-tool-call signatures live on the calls.
    signature: str | None = None
    #: Logged when the reply is empty -- usually a token limit or a content
    #: filter, and invisible otherwise.
    finish_reason: str | None = None


class LLMProvider:
    name = "base"

    def generate(self, system_prompt: str, history: list[dict], tools: list) -> ModelReply:
        raise NotImplementedError
