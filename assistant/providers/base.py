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

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("wanas.providers")


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
    #:
    #: A string for Gemini's thought signatures, a list for OpenRouter's
    #: `reasoning_details` blocks -- the shape is the provider's business, and
    #: it round-trips through the JSON history column either way. Each
    #: provider recognises only its own and drops the other, so a session that
    #: changed providers mid-life cannot feed one protocol's blob to the
    #: other.
    signature: str | list | None = None
    #: Logged when the reply is empty -- usually a token limit or a content
    #: filter, and invisible otherwise.
    finish_reason: str | None = None


@dataclass
class ImageReading:
    """What a vision pass is allowed to conclude about a customer's photo.

    Deliberately narrow. The model may point at a product **that was given to
    it in a list**, or say it recognised nothing -- it may not name a garment
    the shop might sell, invent a colour, or quote anything. Everything the
    customer is eventually told still comes from a tool, exactly as it does for
    text; this is a hint about *which* tool to call, not an answer.
    """

    #: A product_id from the shortlist handed to the provider, or None.
    product_id: str | None = None
    #: 0.0-1.0. The runtime, not the provider, decides what is high enough.
    confidence: float = 0.0
    #: A short, plain description of the garment in the photo. Used to ask a
    #: better question when nothing matched -- never repeated as a claim about
    #: stock.
    description: str = ""
    #: True when the photo is not a garment at all (a receipt, a screenshot, a
    #: person, a parcel). Those are support, not shopping.
    is_garment: bool = True


@dataclass
class SizeChartReading:
    """The numbers a vision pass read off a size-chart picture.

    Read, not decided. A wrong measurement here is a customer ordering the
    wrong size and sending it back, so nothing produced by this ever reaches a
    customer un-reviewed: the dashboard shows it as a filled-in form for a
    staff member to correct and confirm. `confidence` and `notes` exist to
    help them judge, not to gate anything automatically.
    """

    #: [{"key", "label_en", "label_ar"}] -- the columns, in the order read.
    measurements: list[dict] = field(default_factory=list)
    #: {"S": {"width": 54, ...}, ...} -- only sizes the picture actually shows.
    sizes: dict[str, dict] = field(default_factory=dict)
    #: "cm" or "in"; "cm" when the picture does not say.
    unit: str = "cm"
    #: 0.0-1.0, the model's own view of how legible the chart was.
    confidence: float = 0.0
    #: Anything the reader could not resolve, phrased for a staff member.
    notes: str = ""


_KEY_SAFE = re.compile(r"[^a-z0-9]+")


def _measurement_key(raw: str, fallback: str) -> str:
    key = _KEY_SAFE.sub("_", str(raw or "").strip().lower()).strip("_")
    return key or fallback


def normalise_chart_reading(parsed: dict, *, sizes: list[str]) -> SizeChartReading:
    """Turn a provider's raw JSON into a `SizeChartReading` that is safe to show.

    Shared by every provider so the guarantees are the same wherever the
    reading came from, and enforced here rather than trusted from the model:

    - a size the product does not sell is dropped (case-insensitively matched
      back to the product's own spelling, so "s" becomes "S");
    - a value that is not a number is dropped, never coerced -- a measurement
      the model could not read must arrive as blank, not as 0;
    - a column no surviving size has a value for is dropped with it.

    Anything dropped is a cell the staff member fills in, which is the whole
    point of the reading being a form rather than an answer.
    """
    wanted = {str(s).strip().lower(): str(s).strip() for s in sizes if str(s).strip()}

    measurements = []
    seen: set[str] = set()
    for index, row in enumerate(parsed.get("measurements") or []):
        if not isinstance(row, dict):
            continue
        key = _measurement_key(row.get("key") or row.get("label_en"), f"measurement_{index + 1}")
        if key in seen:
            continue
        seen.add(key)
        measurements.append(
            {
                "key": key,
                "label_en": str(row.get("label_en") or key.replace("_", " ").title()).strip(),
                "label_ar": str(row.get("label_ar") or "").strip(),
            }
        )

    out_sizes: dict[str, dict] = {}
    for name, values in (parsed.get("sizes") or {}).items():
        label = wanted.get(str(name).strip().lower()) if wanted else str(name).strip()
        if not label or not isinstance(values, dict):
            continue
        row = {}
        for key, value in values.items():
            key = _measurement_key(key, "")
            if key not in seen:
                continue
            try:
                row[key] = float(value)
            except (TypeError, ValueError):
                continue  # unreadable stays blank; never 0
            if row[key].is_integer():
                row[key] = int(row[key])
        if row:
            out_sizes[label] = row

    used = {key for row in out_sizes.values() for key in row}
    measurements = [m for m in measurements if m["key"] in used]

    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    unit = str(parsed.get("unit") or "cm").strip().lower()
    return SizeChartReading(
        measurements=measurements,
        sizes=out_sizes,
        unit=unit if unit in {"cm", "in"} else "cm",
        confidence=max(0.0, min(1.0, confidence)),
        notes=str(parsed.get("notes") or "").strip(),
    )


#: The buckets a public Instagram comment sorts into.
#:
#: This used to be six, and one of them -- `important` -- absorbed every real
#: question a shopper can ask: price, stock, size, colour, fabric, and "where
#: is my order". They all got one identical public line and one identical DM,
#: which is what made the surface read as a bot. They are now separate
#: because they deserve *different words*, not because the routing differs;
#: `assistant/comment_replies.py` owns the words and the caller owns the
#: routing.
#:
#: `complaint` stays split from `negative` because the two deserve opposite
#: treatment: a hater ignored is fine, a paying customer ignored *in public*
#: is the worst outcome available on this surface. `order_status` is split
#: from both because the person is neither angry nor browsing -- they are
#: waiting on something they already paid for, and that is a third voice.
#: `tag_friend` is split from `other` because the person worth answering is
#: the friend about to get the notification, not the tagger. `spam` is the
#: only category that gets no public reply at all -- answering a scam bot
#: publishes it to everyone reading the post.
#:
#: Nothing here writes to the comment publicly; that stays fixed wording the
#: caller owns, picked from a bank rather than a single line.
COMMENT_CATEGORIES = (
    # real questions about a product, one per thing a shopper actually asks
    "price",
    "availability",
    "size",
    "variant",
    "product_info",
    # a customer who has already bought
    "order_status",
    "complaint",
    # sentiment and noise
    "positive",
    "negative",
    "tag_friend",
    "spam",
    "other",
)

#: Categories this used to have, mapped onto the ones that replaced them. A
#: model pinned to an older prompt (or a hand-written fixture) can still
#: answer `important`/`neither` and be routed sensibly instead of silently
#: becoming a no-op.
LEGACY_COMMENT_CATEGORIES = {"important": "other", "neither": "other"}

#: The twelve categories above, grouped into the four buckets a person
#: scanning a list actually sorts by. The fine category is why a particular
#: reply was chosen; this is how someone decides what to read first, and the
#: dashboard files comments under it.
#:
#: `complaint` sits with `negative` rather than with the questions even though
#: it opens a DM like one: a paying customer with a problem, said in public,
#: is the thing to read before anything else on the list. `tag_friend` sits
#: with `positive` because that is what it is -- somebody bringing a friend to
#: the post. `spam` and `other` are neither, and lumping them into any of the
#: three would quietly inflate whichever one they landed in.
COMMENT_SENTIMENTS = ("question", "positive", "negative", "other")

COMMENT_SENTIMENT = {
    "price": "question",
    "availability": "question",
    "size": "question",
    "variant": "question",
    "product_info": "question",
    "order_status": "question",
    "complaint": "negative",
    "negative": "negative",
    "positive": "positive",
    "tag_friend": "positive",
    "spam": "other",
    "other": "other",
}


def comment_sentiment(category: str | None) -> str:
    """The bucket a category belongs to, defaulting to `other`.

    Never raises and never invents a fifth bucket: an unknown category is a
    classifier answer nobody has taught this map about yet, and filing it
    under `other` shows it in the list rather than dropping it from every
    filter at once.
    """
    return COMMENT_SENTIMENT.get(category or "", "other")


@dataclass
class CommentClassification:
    #: One of COMMENT_CATEGORIES. Anything else the provider returns is
    #: coerced by the caller -- an old name through
    #: LEGACY_COMMENT_CATEGORIES, anything unrecognised to "other", which
    #: answers politely and asks rather than guessing at engagement.
    category: str = "other"


def classification_from(parsed: dict) -> CommentClassification:
    """One classifier answer, turned into a category -- or refused.

    The distinction this draws is the whole point of it, and getting it wrong
    is what let two openly angry public comments be answered as small talk:

    * A category name nobody here recognises is still an *answer*. The model
      read the comment and picked something; `other` files it under a real
      bucket and the comment is answered politely, which is the right outcome
      for a name this map has not been taught yet.
    * A **missing or empty** category is not an answer at all. It is what a
      truncated, refused or empty completion leaves behind, and coercing it to
      `other` is indistinguishable from the model having judged the comment
      unremarkable -- so a furious comment gets a friendly DM, raises no
      alert, and the owner never hears about it. That is refused here, and
      `assistant/channels/instagram.py` turns the refusal into silence plus a
      `classifier_unavailable` alert: nothing published, and somebody told.

    Shared by both providers so the two doors cannot drift apart.
    """
    category = str(parsed.get("category") or "").strip().lower()
    if not category:
        raise ProviderError("classification reply carried no category")
    category = LEGACY_COMMENT_CATEGORIES.get(category, category)
    if category not in COMMENT_CATEGORIES:
        log.warning("classify_comment returned an unknown category %r; treating as other", category)
        category = "other"
    return CommentClassification(category=category)


class LLMProvider:
    name = "base"

    #: Media capabilities, declared rather than discovered: the runtime has to
    #: decide whether to transcribe or to hand a voice note to a person
    #: *before* it spends a call finding out.
    supports_audio = False
    supports_vision = False

    def generate(self, system_prompt: str, history: list[dict], tools: list) -> ModelReply:
        raise NotImplementedError

    def transcribe(self, audio: bytes, mime_type: str, *, hint: str = "") -> str:
        """A voice note as text, in the language it was spoken.

        Providers that cannot do this raise `ProviderError(kind="unsupported")`
        and the caller falls back to handing the message to a person -- which
        is what happened to every voice note before this existed.
        """
        raise ProviderError(f"{self.name} cannot transcribe audio", kind="unsupported")

    def inspect_image(
        self, image: bytes, mime_type: str, *, catalog: list[dict]
    ) -> ImageReading:
        """Read a customer's photo against a shortlist of real products.

        `catalog` is a list of ``{"product_id", "name", "category", "colors"}``
        built by the caller from the actual catalog. Matching is only ever
        against that list; there is no path by which the model can answer with
        a product the shop does not have.
        """
        raise ProviderError(f"{self.name} cannot read images", kind="unsupported")

    def read_size_chart(self, image: bytes, mime_type: str, *, sizes: list[str]) -> SizeChartReading:
        """The measurements printed on a size-chart picture, as data.

        `sizes` is what the product actually has, so a chart listing sizes the
        product does not sell contributes nothing. Providers that cannot do
        this raise `ProviderError(kind="unsupported")` and the caller falls
        back to the staff member typing the table -- which is what happened to
        every uploaded chart before this existed.
        """
        raise ProviderError(f"{self.name} cannot read images", kind="unsupported")

    def classify_comment(self, text: str) -> CommentClassification:
        """Sort one public Instagram comment into COMMENT_CATEGORIES.

        Cheap and fast on purpose: a single JSON-only call with no tools and
        no conversation history, the same pattern `inspect_image` uses --
        comment volume can run far higher than DM volume, so this must never
        route through the multi-round tool-calling agent. The caller decides
        what each category *does*; this only classifies.
        """
        raise ProviderError(f"{self.name} cannot classify comments", kind="unsupported")
