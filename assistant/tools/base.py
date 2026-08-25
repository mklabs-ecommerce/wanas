"""Tool plumbing: the context, the spec, the registry, and argument validation.

Rules that apply to every tool (15-tool-contracts.md):

* Returns a JSON object, never a string of prose. The model does the phrasing.
* Never raises. A failure returns {"error": "<code>", ...} -- a crash inside a
  tool ends the customer's conversation, which is worse than any error message.
* `error` is a stable code, not a sentence. The model maps codes to wording;
  changing a code changes behaviour, changing wording does not.
* The channel identity is injected by the runtime, never passed by the model.
  A model that could supply it could read another customer's cart.
* Money is a number in EGP, never a formatted string.
* Unknown or extra arguments are rejected rather than ignored.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

log = logging.getLogger("wanas.tools")


@dataclass
class ToolContext:
    """Everything a tool needs that the model is not allowed to supply."""

    session: Session
    channel: str
    external_id: str
    #: Image paths collected from this turn's tool results, handed to the
    #: channel adapter alongside the reply text. The model writes the words;
    #: it never emits a path into the message body.
    attachments: list[str] = field(default_factory=list)
    #: Images already delivered earlier in this conversation (read from the
    #: session history before the turn starts). The default image policy
    #: never re-sends one of these -- credit waste, not helpfulness.
    sent_images: set[str] = field(default_factory=set)
    #: This turn's history so far, same list object the agent loop appends
    #: to. Lets a read-only catalog call be served from an identical call
    #: already answered earlier in the conversation instead of hitting the
    #: database again.
    history: list[dict] = field(default_factory=list)
    #: A tappable picker the adapter should send instead of a plain text
    #: reply. Set by a tool, never by the model: the options have to come from
    #: the database for the same reason a price does.
    interactive: dict | None = None

    def offer(self, payload: dict) -> bool:
        """Attach a picker to this reply. First one wins.

        A turn produces one message, so a second picker would silently replace
        the first -- and the tool that set it would have no idea. Refusing is
        the honest outcome and the caller can say so in words instead.
        """
        if not payload or self.interactive is not None:
            return False
        self.interactive = payload
        return True

    def attach(self, path: str, *, force: bool = False) -> bool:
        """Add an image to this turn's reply, once.

        `force` bypasses the cross-turn `sent_images` check but never the
        same-turn de-dup -- it exists for the size chart (the answer itself)
        and for an explicit "send more photos" request, where resending
        something already shown is the customer's actual ask, not spam.
        """
        if not (isinstance(path, str) and path) or path in self.attachments:
            return False
        if not force and path in self.sent_images:
            return False
        self.attachments.append(path)
        return True


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    #: JSON-schema style, kept vendor-neutral. The provider layer translates.
    properties: dict[str, dict]
    required: tuple[str, ...]
    handler: Callable[..., dict]
    #: The error code for an absent or blank required argument. `bad_arguments`
    #: everywhere except confirm_order, whose contract names `missing_fields`
    #: with the field list -- the model's cue to ask for the one that is
    #: missing rather than re-send the whole call.
    missing_error: str = "bad_arguments"


REGISTRY: dict[str, ToolSpec] = {}


def tool(
    name: str,
    description: str,
    properties: dict | None = None,
    required: tuple[str, ...] = (),
    missing_error: str = "bad_arguments",
):
    def decorator(fn: Callable[..., dict]) -> Callable[..., dict]:
        REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            properties=properties or {},
            required=required,
            handler=fn,
            missing_error=missing_error,
        )
        return fn

    return decorator


_PY_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def validate_arguments(spec: ToolSpec, arguments: dict) -> dict | None:
    """Returns a bad_arguments payload, or None when the call is acceptable.

    Rejecting rather than ignoring an unknown argument is what stops a
    hallucinated `client_id` or `price` looking like it was honoured.
    """
    if not isinstance(arguments, dict):
        return {"error": "bad_arguments", "detail": "arguments must be an object"}

    unknown = sorted(set(arguments) - set(spec.properties))
    if unknown:
        return {"error": "bad_arguments", "detail": f"unknown argument(s): {', '.join(unknown)}"}

    # Blank counts as missing: whitespace is not an address, and a required
    # field that is present-but-empty is exactly what a model produces when it
    # is guessing at a value it was never told.
    missing = [
        name
        for name in spec.required
        if arguments.get(name) is None or (isinstance(arguments[name], str) and not arguments[name].strip())
    ]
    if missing:
        if spec.missing_error == "missing_fields":
            return {"error": "missing_fields", "fields": missing}
        return {"error": "bad_arguments", "detail": f"missing required argument(s): {', '.join(missing)}"}

    for name, value in arguments.items():
        if value is None:
            continue
        expected = spec.properties[name].get("type")
        py_type = _PY_TYPES.get(expected)
        if py_type is None:
            continue
        if expected == "integer" and isinstance(value, bool):
            return {"error": "bad_arguments", "detail": f"{name} must be an integer"}
        if expected in {"integer", "number"} and isinstance(value, str):
            # Providers routinely stringify numbers; coerce rather than refuse
            # something the model actually got right.
            try:
                arguments[name] = int(value) if expected == "integer" else float(value)
                continue
            except ValueError:
                return {"error": "bad_arguments", "detail": f"{name} must be a {expected}"}
        if not isinstance(value, py_type):
            return {"error": "bad_arguments", "detail": f"{name} must be a {expected}"}
    return None


#: Read-only catalog lookups whose answer for the same arguments does not
#: meaningfully change inside one conversation. Cart, order and handoff tools
#: are never cached -- those have to run for real every time.
#: `ask_governorate` is *not* cacheable even though it only reads: its whole
#: purpose is to put a picker in front of the customer, and a cached copy
#: would return the rows with no picker attached.
CACHEABLE_TOOLS = {"get_categories", "get_products", "get_variants", "get_size_chart", "get_shipping_fee"}


def _cached_result(ctx: ToolContext, name: str, arguments: dict) -> dict | None:
    """An identical call already answered earlier in this conversation, if any.

    Avoids "get_variants -> ok / get_variants -> ok" for the same product
    across turns: the customer asking about L right after XL should not cost a
    second round trip when the first answer already named every size. Reads
    only `ctx.history`, which is the same list the agent loop is building this
    turn, so this also catches a duplicate within one turn, not only across
    them. Never applied to a tool with side effects -- those must always run.
    """
    if name not in CACHEABLE_TOOLS:
        return None
    for index, message in enumerate(ctx.history):
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if call.get("name") != name or (call.get("arguments") or {}) != arguments:
                continue
            call_id = call.get("id")
            following = ctx.history[index + 1] if index + 1 < len(ctx.history) else None
            if not following or following.get("role") != "tool_results":
                continue
            for result in following.get("results") or []:
                if result.get("id") != call_id:
                    continue
                content = result.get("content")
                if isinstance(content, dict) and "error" not in content:
                    return content
    return None


def call_tool(ctx: ToolContext, name: str, arguments: dict | None) -> dict:
    """Dispatch. Never raises: an unexpected exception becomes an error dict."""
    spec = REGISTRY.get(name)
    if spec is None:
        return {"error": "unknown_tool", "tool": name}

    arguments = dict(arguments or {})
    invalid = validate_arguments(spec, arguments)
    if invalid is not None:
        return invalid

    cached = _cached_result(ctx, name, arguments)
    if cached is not None:
        log.info("tool %s(%s) served from session cache, not re-fetched", name, arguments)
        result = dict(cached)
    else:
        try:
            result = spec.handler(ctx, **arguments)
        except Exception as exc:  # a tool must not end the conversation
            log.exception("tool %s failed", name)
            return {"error": "tool_failed", "tool": name, "detail": str(exc)}

        if not isinstance(result, dict):  # pragma: no cover - guards a coding error
            log.error("tool %s returned %r, not an object", name, type(result))
            return {"error": "tool_failed", "tool": name, "detail": "tool did not return an object"}

    # A tool signals "the customer explicitly asked for more photos" with this
    # internal marker rather than as part of the answer -- popped here so it
    # never reaches the model as data to explain.
    more_images = bool(result.pop("_more_images", False))
    color = result.pop("_image_color", None)
    _collect_images(ctx, result, more_images=more_images, color=color)
    return result


#: How many product photos an ordinary "show me the product" turn may carry.
#: One, on purpose: a picture is a credit cost, and the customer asked to see
#: the product, not a gallery of it. `_more_images` below is the only way to
#: get past this.
MAX_PRODUCT_IMAGES = 1

#: How many extra photos an explicit "show me more" may add, on top of
#: whatever the conversation has already sent.
MAX_EXTRA_IMAGES = 2


def _candidate_images(result: dict, color: str | None = None) -> list[str]:
    """Every photo a tool result could offer, the asked-for colour first.

    For a product that comes in three colours, one photo of each is a more
    useful default than three angles of the same one -- so colour variety is
    what `more_images` reaches for, not just "the next file in the list".

    `color` is the colourway the customer is actually talking about, when the
    tool was able to name one. Without it the list simply starts at whichever
    colour happens to be first, which is how "عايز الهودي الزيتي" got answered
    with a photo of the black one.
    """
    color_images = result.get("color_images")
    picked: list[str] = []
    if isinstance(color_images, dict):
        wanted = _matching_color(color_images, color)
        keys = [wanted] + [c for c in color_images if c != wanted] if wanted else list(color_images)
        for key in keys:
            paths = color_images.get(key)
            if isinstance(paths, list) and paths:
                picked.append(paths[0])

    if not picked:
        # A product Shopify has not split by colour falls back to the
        # unlabelled set -- an unlabelled photo is fine, a wrong colourway
        # labelled confidently is not.
        images = result.get("images")
        if isinstance(images, list):
            picked = [p for p in images if isinstance(p, str)]
    return picked


def _matching_color(color_images: dict, color: str | None) -> str | None:
    """The `color_images` key the customer's word refers to, or None.

    Case- and spacing-insensitive because the colour reaches here as the model
    typed it, not as an option value: "olive" must find "Olive", and
    "camel brown" must find "Camel Brown".
    """
    if not isinstance(color, str) or not color.strip():
        return None
    needle = " ".join(color.split()).casefold()
    for key in color_images:
        if isinstance(key, str) and " ".join(key.split()).casefold() == needle:
            return key
    return None


def _collect_images(
    ctx: ToolContext, result: dict, *, more_images: bool = False, color: str | None = None
) -> None:
    """Turn image paths in a tool result into real attachments.

    Tools report images in two different shapes and both are part of the tool
    contracts: `image` is a single path (a size chart -- the answer itself, so
    it is always sent, even if an identical chart went out earlier -- asking
    for it again is asking to see it again), while `images` / `color_images`
    are the product's photos, which follow the strict budget below and are
    never repeated unless nothing unseen is left to show.
    """
    image = result.get("image")
    if isinstance(image, str) and image:
        ctx.attach(image, force=True)

    candidates = _candidate_images(result, color)
    if not candidates:
        return

    if more_images:
        # Unseen photos first; only fall back to one already sent if the
        # product genuinely has nothing left to show -- the customer asked
        # for more, so silence is the wrong answer, but a repeat is still a
        # last resort.
        ranked = [p for p in candidates if p not in ctx.sent_images] + [
            p for p in candidates if p in ctx.sent_images
        ]
        added = 0
        for path in ranked:
            if added >= MAX_EXTRA_IMAGES:
                break
            if ctx.attach(path, force=True):
                added += 1
        return

    # What counts as "already answered" is the colourway being talked about,
    # not the product. Showing the black hoodie and then being asked for the
    # olive one is a new question, and the whole product used to count as
    # answered here -- so the reply that should have carried the olive photo
    # carried none at all.
    named = color and _matching_color(result.get("color_images") or {}, color)
    answered = candidates[:1] if named else candidates
    if any(path in ctx.sent_images for path in answered):
        # The default request is "show me the product", already answered --
        # substituting a different colour nobody asked for is still fetching
        # another image, which is exactly what a plain request must not do.
        return

    for path in candidates[:MAX_PRODUCT_IMAGES]:
        ctx.attach(path)


def tool_specs() -> list[ToolSpec]:
    return [REGISTRY[name] for name in sorted(REGISTRY)]


def load_all() -> None:
    """Import the tool modules so their decorators run."""
    from assistant.tools import cart_tools, catalog_tools, order_tools, support_tools  # noqa: F401


def reset_registry_for_tests() -> None:  # pragma: no cover - test helper
    REGISTRY.clear()
