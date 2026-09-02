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
    #: What each attached photo actually *is*, keyed by path:
    #: `{"label", "product_id", "name", "color"}`. A reply that sends one
    #: photo per colourway leaves the customer looking at four pictures and
    #: only one way to point at one of them: long-press the photo they want
    #: and reply to it. Resolving that quote back to a colour is only possible
    #: if something wrote down which colour each picture was, and until this
    #: existed nothing did -- the paths were collected as a bare list and the
    #: colour that chose them was discarded one line later.
    #:
    #: `label` is the wording a quote is annotated with; `product_id` is what
    #: makes the *reply itself* a product reference, so pointing at a photo of
    #: a product the conversation had moved away from moves it back. See
    #: `assistant/quoting.py`, which reads both.
    attachment_labels: dict[str, dict] = field(default_factory=dict)
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
    #: Set by a tool that has already said everything this turn needs to say,
    #: on its own and in words the shop wrote rather than words a model chose.
    #: The agent stops the loop there and sends nothing further: the value is
    #: the reason, for the log. `confirm_order` is the case that put it here
    #: -- the order confirmation is composed and delivered by
    #: `domain/services/notifications.py`, so a model reply after it is a
    #: second message about the same order landing on the customer's phone.
    end_turn: str | None = None

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

    def attach(self, path: str, *, force: bool = False, label: dict | None = None) -> bool:
        """Add an image to this turn's reply, once.

        `force` bypasses the cross-turn `sent_images` check but never the
        same-turn de-dup -- it exists for the size chart (the answer itself)
        and for an explicit "send more photos" request, where resending
        something already shown is the customer's actual ask, not spam.

        `label` says what the picture is of, for `attachment_labels` above.
        Optional because a photo the tool layer could not describe is still a
        photo -- it just cannot be named, or read as a product reference,
        if the customer replies to it.
        """
        if not (isinstance(path, str) and path) or path in self.attachments:
            return False
        if not force and path in self.sent_images:
            return False
        self.attachments.append(path)
        if label and label.get("label"):
            self.attachment_labels[path] = dict(label)
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


#: Catalog calls whose arguments name exactly one product.
_PRODUCT_CALLS = {"get_variants", "get_size_chart"}


def _reference_from_call(history: list[dict], index: int, call: dict) -> dict | None:
    """One tool call read as "the conversation is about this product", or None.

    A call only counts once its *result* came back clean: an id the model
    made up names no product, and treating the attempt as a reference would
    make a hallucinated id the thing every later question resolves against.
    """
    name = call.get("name")
    if name not in _PRODUCT_CALLS and name != "get_products":
        return None
    following = history[index + 1] if index + 1 < len(history) else None
    if not following or following.get("role") != "tool_results":
        return None
    for result in following.get("results") or []:
        if result.get("id") != call.get("id"):
            continue
        content = result.get("content")
        if not isinstance(content, dict) or "error" in content:
            return None
        if name == "get_products":
            return _sole_search_hit(content)
        product_id = (call.get("arguments") or {}).get("product_id")
        if not (isinstance(product_id, str) and product_id.strip()):
            return None
        # `title` is what get_size_chart calls it; either way the name comes
        # from the database, never from a model's sentence.
        found = content.get("name") or content.get("title")
        color = (call.get("arguments") or {}).get("color")
        return {
            "product_id": product_id,
            "name": found if isinstance(found, str) and found else None,
            "color": color if isinstance(color, str) and color else None,
        }
    return None


def _sole_search_hit(content: dict) -> dict | None:
    """A `get_products` result that found exactly one product, or None.

    `get_products` answers with a *list*, and "which of these" is an ambiguity
    to keep rather than one to resolve -- which is why it was excluded from
    this walk entirely. But a search that matched exactly one product is not
    ambiguous, it is an answer, and it is the ordinary shape of a customer
    naming something: "عندكم الجاكيت الوركر؟" searches, finds one, and the
    conversation has moved. Excluding it meant a follow-up question after the
    customer switched products still resolved to the product *before* the
    switch, which is the one wrong answer implicit resolution must not give.
    """
    products = content.get("products")
    if not isinstance(products, list) or len(products) != 1:
        return None
    only = products[0]
    if not isinstance(only, dict):
        return None
    product_id = only.get("product_id")
    if not (isinstance(product_id, str) and product_id.strip()):
        return None
    found = only.get("name")
    return {
        "product_id": product_id,
        "name": found if isinstance(found, str) and found else None,
        "color": None,
    }


def last_product(history: list[dict]) -> dict | None:
    """The one product a conversation is currently about, or None.

    The **newest** unambiguous reference, as `{"product_id", "name", "color"}`.
    Deterministic and read-only -- one backwards walk, no model call, nothing
    summarised. Three things count as a reference, and the most recent of them
    wins outright:

    * a `get_variants`/`get_size_chart` call that named a product and got a
      real answer back;
    * a `get_products` search that matched exactly **one** product -- the
      ordinary shape of a customer naming something new. A search with two or
      more hits is deliberately not a reference: "which of these" is an
      ambiguity to keep;
    * a customer message that replied to a **photo**, which carries the
      product that photo was of (`refers_to`, from
      `assistant/quoting.py::referenced_product`). Pointing at a picture is
      how a customer changes the subject without typing a name.

    It reads the **stored** history rather than the compacted view the model
    is sent (`assistant/context.py`), and that is the point: compaction drops
    tool results, which is exactly where the product id is written down. So
    this can still name the product in a turn where the model itself has lost
    track of it -- which is the situation every caller exists for.

    Because it is what an omitted `product_id` resolves to, "newest" is not a
    detail: the whole risk in answering "what sizes?" without asking which
    product is answering it about the product before last. Every way a
    customer can move the conversation has to move this with it.
    """
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        role = message.get("role")
        if role == "user":
            refers_to = message.get("refers_to")
            product_id = (refers_to or {}).get("product_id") if isinstance(refers_to, dict) else None
            if isinstance(product_id, str) and product_id.strip():
                return {
                    "product_id": product_id,
                    "name": refers_to.get("name"),
                    "color": refers_to.get("color"),
                }
            continue
        if role != "assistant":
            continue
        for call in reversed(message.get("tool_calls") or []):
            found = _reference_from_call(history, index, call)
            if found is not None:
                return found
    return None


#: Tools whose `product_id` may be left out to mean "the one we are already
#: talking about".
#:
#: `get_size_chart` is here because it is the tool a customer reaches through
#: a question ("مش عارف مقاسي") rather than through a product id, so requiring
#: the id turned a sizing question into a stall.
#:
#: `get_variants` is here for the same reason one step earlier: "المقاسات
#: إيه؟" and "بيجي بألوان إيه؟" are follow-ups to a product already on the
#: table, and the id for it lives in a tool call `assistant/context.py`
#: compacted away turns ago. Refusing them made the bot stop and ask which
#: product -- a question the customer had already answered, which is the
#: single most obvious way for it to read as not listening.
#:
#: The wrong-product risk this widening carries is answered by `last_product`
#: tracking the *newest* reference rather than by keeping this set small: a
#: customer who has moved on has moved it on too, by naming the new product,
#: by looking it up, or by replying to its photo.
_IMPLICIT_PRODUCT_TOOLS = {"get_size_chart", "get_variants"}


def _resolve_implicit_product(ctx: ToolContext, name: str, arguments: dict) -> None:
    """Fill in an omitted `product_id` from the conversation, in place.

    Done here, before anything else looks at the call, so the arguments a
    handler runs on and the arguments `_cached_result` keys on are the same
    concrete ones. Resolving inside the handler instead would leave the cache
    keyed on `{}` -- "whatever we were talking about" -- and hand product A's
    chart to a customer now asking about product B.

    Only an *absent* id is resolved. A wrong one is left to be refused,
    because quoting a neighbouring product's chart is confident, precise,
    wrong numbers, and sizing wrong causes a return.
    """
    if name not in _IMPLICIT_PRODUCT_TOOLS:
        return
    given = arguments.get("product_id")
    if isinstance(given, str) and given.strip():
        return
    recent = last_product(ctx.history)
    if recent is not None:
        arguments["product_id"] = recent["product_id"]
        log.info("%s called with no product_id, resolved to %s", name, recent["product_id"])


def call_tool(ctx: ToolContext, name: str, arguments: dict | None) -> dict:
    """Dispatch. Never raises: an unexpected exception becomes an error dict."""
    spec = REGISTRY.get(name)
    if spec is None:
        return {"error": "unknown_tool", "tool": name}

    arguments = dict(arguments or {})
    _resolve_implicit_product(ctx, name, arguments)
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
    chart_image = result.pop("_size_chart_image", None)
    # The *resolved* id, so a photo attached by an implicitly-resolved call is
    # labelled with the product it actually came from.
    _collect_images(
        ctx, result, more_images=more_images, color=color, product_id=arguments.get("product_id")
    )
    # After the product photo, and deliberately *not* forced: a chart is the
    # same picture every time, so the cross-conversation `sent_images` check
    # is exactly the rule wanted here -- it rides along the first time a
    # product's sizes come up and never again. An explicit get_size_chart
    # still forces it, because asking for it again is asking to see it again.
    if isinstance(chart_image, str) and chart_image:
        ctx.attach(chart_image, label=_chart_label(result, arguments.get("product_id")))
    return result


#: How many product photos an ordinary "show me the product" turn may carry.
#: One, on purpose: a picture is a credit cost, and the customer asked to see
#: the product, not a gallery of it. `_more_images` below is the only way to
#: get past this.
MAX_PRODUCT_IMAGES = 1

#: How many extra photos an explicit "show me more" may add for a product the
#: catalogue never split by colour -- where "more" can only mean another angle
#: of the one garment, and two of those is already everything worth sending.
MAX_EXTRA_IMAGES = 2

#: The ceiling for a colour-split product, where "ابعتلي صور كل الألوان" has a
#: right answer and it is **one photo per colourway**. A flat two used to apply
#: here as well, so a four-colour product answered a request for all four with
#: two -- and the customer, reasonably, asked again for the rest. That second
#: ask is the one that produced no photo at all, because by then the model had
#: been told the product was already shown. The budget follows the product now
#: rather than a constant that knows nothing about it. Six is where a gallery
#: stops being an answer and starts being a screenful of notifications.
MAX_COLOR_IMAGES = 6


def _extra_budget(result: dict, candidates: list[str]) -> int:
    """How many photos one explicit "show me more" may send.

    Colour-split: as many as it has colourways, because that is the question
    -- `_candidate_images` already returns exactly one photo per colour, so
    the length of the list *is* the colour count. Not split: two more angles.
    """
    color_images = result.get("color_images")
    if isinstance(color_images, dict) and color_images:
        return min(len(candidates), MAX_COLOR_IMAGES)
    return MAX_EXTRA_IMAGES


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


def _image_labels(result: dict, product_id: str | None = None) -> dict[str, dict]:
    """What each photo in a tool result is of, keyed by path.

    Built from `color_images`, which is the only place the colourway of a
    picture is ever written down -- `_candidate_images` reads it to *choose*
    a photo and then returns bare paths, so the colour was known and thrown
    away at exactly the moment it became worth keeping.

    Every value carries the product as well as the wording, because a photo
    is two different facts at once: `label` is what a quote of it is annotated
    with, and `product_id` is what makes replying to it a *reference to that
    product*. Both come straight out of the database. Neither is shown to the
    customer or sent to the model as data.
    """
    name = result.get("name") or result.get("title")
    name = name if isinstance(name, str) and name.strip() else None
    product_id = product_id or result.get("product_id")
    product_id = product_id if isinstance(product_id, str) and product_id.strip() else None
    labels: dict[str, dict] = {}

    def _entry(label: str, color: str | None) -> dict:
        return {"label": label, "product_id": product_id, "name": name, "color": color}

    color_images = result.get("color_images")
    if isinstance(color_images, dict):
        for key, paths in color_images.items():
            if not (isinstance(key, str) and key.strip()) or not isinstance(paths, list):
                continue
            entry = _entry(f"{name} ({key})" if name else key, key)
            for path in paths:
                if isinstance(path, str) and path:
                    labels.setdefault(path, entry)

    if name:
        # A product with no colour split still gets a name, so a reply to one
        # of two extra angles at least resolves to the right product.
        images = result.get("images")
        if isinstance(images, list):
            for path in images:
                if isinstance(path, str) and path:
                    labels.setdefault(path, _entry(name, None))
    return labels


def _chart_label(result: dict, product_id: str | None = None) -> dict | None:
    """"<product> size chart", when the result names a product.

    A size chart is the one attachment a customer replies to meaning
    something other than "this colour" -- they are asking about the numbers.
    Saying so is what stops the next turn reading it as a colour choice. It
    still names the product: a customer scrolling back to an older chart and
    asking about it has told you which product they mean.
    """
    name = result.get("title") or result.get("name")
    if not (isinstance(name, str) and name.strip()):
        return None
    product_id = product_id if isinstance(product_id, str) and product_id.strip() else None
    return {
        "label": f"{name} size chart",
        "product_id": product_id,
        "name": name,
        "color": None,
    }


def _collect_images(
    ctx: ToolContext,
    result: dict,
    *,
    more_images: bool = False,
    color: str | None = None,
    product_id: str | None = None,
) -> None:
    """Turn image paths in a tool result into real attachments.

    Tools report images in two different shapes and both are part of the tool
    contracts: `image` is a single path (a size chart -- the answer itself, so
    it is always sent, even if an identical chart went out earlier -- asking
    for it again is asking to see it again), while `images` / `color_images`
    are the product's photos, which follow the strict budget below and are
    never repeated unless nothing unseen is left to show.
    """
    labels = _image_labels(result, product_id)

    image = result.get("image")
    if isinstance(image, str) and image:
        ctx.attach(image, force=True, label=labels.get(image) or _chart_label(result, product_id))

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
        budget = _extra_budget(result, candidates)
        added = 0
        for path in ranked:
            if added >= budget:
                break
            if ctx.attach(path, force=True, label=labels.get(path)):
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
        ctx.attach(path, label=labels.get(path))


def tool_specs() -> list[ToolSpec]:
    return [REGISTRY[name] for name in sorted(REGISTRY)]


def load_all() -> None:
    """Import the tool modules so their decorators run."""
    from assistant.tools import cart_tools, catalog_tools, order_tools, support_tools  # noqa: F401


def reset_registry_for_tests() -> None:  # pragma: no cover - test helper
    REGISTRY.clear()
