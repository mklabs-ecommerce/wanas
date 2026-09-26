"""Showing the merchandise: the product photographs a reply carries.

A photograph used to reach the customer only when the model chose to call
`get_variants` (or a search happened to land on exactly one product). The
prompt asked it to, for every reply about a product, and it did not do so
reliably: it answered from the search results already in front of it, and a
clothes shop answered in words alone -- the customer had to ask "طب ابعتلي
صورة" before seeing anything. That made showing the goods a habit of the model
rather than a rule of the shop.

So the reply's own words decide, below the model, after it has written them:

* **every catalog product the reply names gets its photograph.** "Named"
  is a fact, not a guess -- the product's name as a tool in this conversation
  returned it (the prompt keeps names in English, exactly as the catalog
  spells them), or its one distinctive word when no other product shares it.
  A name no tool ever returned is never matched, so a product the model made
  up gets no picture, and `photo_claims` still catches the claim;
* **a reply about one product, the first time that product is shown**, also
  carries its other in-stock colourways -- the colour the reply names first --
  up to `FIRST_SHOWING_PHOTOS`. A reply naming several products shows one
  photograph of each, up to `MAX_SHOWCASE_PRODUCTS`;
* the existing image policy is untouched and still applies to all of it: a
  photograph already delivered in this conversation is never sent again
  uninvited, only in-stock colourways are offered, and a turn about an order,
  a delivery or a handoff (`_NOT_MERCHANDISE`) shows nothing.

Nothing here calls a model, and nothing substitutes one product for another:
the product ids come from the stored tool results, and the photographs from
the same catalog read `get_variants` answers with.
"""

from __future__ import annotations

import logging
import re

from assistant import customer_words
from assistant.messages import TOOL_RESULTS
from assistant.photo_claims import _BOTH, _GENERIC_NAME_WORDS
from assistant.tools.base import (
    ToolContext,
    _chart_label,
    _image_labels,
    _matching_color,
    asked_for_colors,
    last_product,
)
from domain.services import catalog, search_terms

log = logging.getLogger("wanas.showcase")

#: Photographs of one product on the first time a reply is about it alone:
#: the colour being talked about, then the other colourways that can actually
#: be bought. Three is what a salesperson holds up, not a catalogue.
FIRST_SHOWING_PHOTOS = 3

#: A reply naming more products than this is a list, and one photograph each
#: of the first few is the showing; the rest are words until the customer
#: picks one.
MAX_SHOWCASE_PRODUCTS = 4

#: Garment photographs the showcase will bring one reply up to, whatever it
#: found. An explicit request for all the colours (`more_images`) is the tool
#: layer's, and is not counted against this.
MAX_SHOWCASE_PHOTOS = 4

#: Turns that are about an order, a delivery, a complaint or a person -- not
#: about the merchandise. A photograph of a hoodie beside "your order has been
#: cancelled" is noise, and beside a handoff it is worse.
_NOT_MERCHANDISE = frozenset(
    {
        "confirm_order",
        "get_my_orders",
        "modify_order_quantity",
        "cancel_order",
        "add_item_to_order",
        "request_item_swap",
        "request_item_add",
        "get_return_terms",
        "submit_feedback",
        "request_human",
        "ask_governorate",
        "get_shipping_fee",
        "get_my_profile",
        "link_client",
    }
)

_CLAUSE = re.compile(r"[.\n،؛!؟?]+")


def known_products(history: list[dict]) -> dict[str, str]:
    """`{name: product_id}` for every product a tool in this conversation
    returned, newest answer winning.

    Read from tool results only -- a search's `products`, a refusal's
    `alternatives`, and the single-product answers -- so a name is something
    the catalog said, never something the model wrote.
    """
    found: dict[str, str] = {}

    def _note(name, product_id) -> None:
        if isinstance(name, str) and name.strip() and isinstance(product_id, str) and product_id:
            found[name.strip()] = product_id

    for message in history:
        if message.get("role") != TOOL_RESULTS:
            continue
        for result in message.get("results") or []:
            content = result.get("content")
            if not isinstance(content, dict):
                continue
            if "error" in content and not content.get("alternatives"):
                continue  # a refusal names nothing -- except its alternatives
            _note(content.get("name"), content.get("product_id"))
            for key in ("products", "alternatives"):
                for entry in content.get(key) or []:
                    if isinstance(entry, dict):
                        _note(entry.get("name"), entry.get("product_id"))
    return found


#: The shop's own name. Half the catalog is "WANAS <garment>", so the word
#: names the brand, not a product: «منتجات WANAS كلها قطن» is not a request to
#: see the one WANAS product this conversation happened to look up.
_BRAND_WORDS = frozenset({"wanas", "wns", "gallery"})


def _distinctive(name: str) -> str:
    """The word of a product name that says *which* product it is, or "".

    The longest Latin word of five letters or more that is neither the kind
    of garment (`photo_claims._GENERIC_NAME_WORDS`) nor the brand: "Ringer",
    "Lightweight", "Cairokee".
    """
    words = [
        w
        for w in re.findall(r"[A-Za-z][A-Za-z-]*", name or "")
        if len(w) >= 5 and w.lower() not in _GENERIC_NAME_WORDS | _BRAND_WORDS
    ]
    return max(words, key=len).lower() if words else ""


def _pattern(words: str) -> re.Pattern:
    """A name as a whole phrase: any spacing or hyphenation between its words,
    and never the middle of a longer word."""
    parts = [re.escape(part) for part in re.split(r"[\s\-]+", words.casefold()) if part]
    return re.compile(r"(?<![0-9a-z])" + r"[\s\-]*".join(parts) + r"(?![0-9a-z])")


def named_products(text: str, known: dict[str, str]) -> list[tuple[str, str, int, int]]:
    """The products a reply names, in the order it names them.

    `(product_id, name, start, end)`, one entry per product. Whole names are
    matched longest first, so "Cairokee T-shirt 2" is never read as a mention
    of "Cairokee T-shirt" too; then a name's one distinctive word
    (`_distinctive` -- "Ringer", "Lightweight") where no other known
    product shares it. A word two products share names neither.
    """
    lowered = (text or "").casefold()
    taken: list[tuple[int, int]] = []
    hits: dict[str, tuple[str, str, int, int]] = {}

    def _free(start: int, end: int) -> bool:
        return all(end <= a or start >= b for a, b in taken)

    for name in sorted(known, key=len, reverse=True):
        for match in _pattern(name).finditer(lowered):
            if not _free(*match.span()):
                continue
            taken.append(match.span())
            product_id = known[name]
            if product_id not in hits or match.start() < hits[product_id][2]:
                hits[product_id] = (product_id, name, *match.span())

    keys: dict[str, set[str]] = {}
    for name, product_id in known.items():
        key = _distinctive(name)
        if key:
            keys.setdefault(key, set()).add(product_id)
    for key, owners in keys.items():
        if len(owners) != 1:
            continue
        (product_id,) = owners
        for match in re.finditer(rf"(?<![a-z]){re.escape(key)}(?![a-z])", lowered):
            if not _free(*match.span()):
                continue
            taken.append(match.span())
            if product_id not in hits:
                name = next(n for n, p in known.items() if p == product_id)
                hits[product_id] = (product_id, name, *match.span())

    return sorted(hits.values(), key=lambda hit: hit[2])


def _clause_around(text: str, start: int, end: int) -> str:
    """The clause a mention sits in -- where a colour beside it would be."""
    left = max((m.end() for m in _CLAUSE.finditer(text, 0, start)), default=0)
    right = _CLAUSE.search(text, end)
    return text[left : right.start() if right else len(text)]


def colour_named(clause: str, colours: list[str]) -> str | None:
    """The colourway a clause names, in English or Arabic, or None.

    Arabic goes through the same vocabulary the search uses
    (`search_terms`), so «الزيتي» is Olive here exactly as it is there.
    """
    lowered = clause.casefold()
    spoken: set[str] = set()
    for token in search_terms.normalize(clause).split():
        spoken.update(search_terms.expand(token))
    for colour in colours:
        if not isinstance(colour, str) or not colour.strip():
            continue
        wanted = search_terms.normalize(colour)
        if _pattern(colour).search(lowered) or wanted in spoken:
            return colour
    return None


def _in_stock_colours(payload: dict) -> list[str]:
    stocked = set(payload.get("in_stock") or [])
    ordered: list[str] = []
    for variant in payload.get("variants") or []:
        colour = variant.get("color")
        if variant.get("variant_id") in stocked and colour and colour not in ordered:
            ordered.append(colour)
    return ordered


def _photos(payload: dict, lead: str | None, stocked: list[str]) -> list[str]:
    """One photo per in-stock colourway, `lead` first; or the product's own
    unlabelled set when the catalog never split it by colour."""
    color_images = payload.get("color_images")
    if isinstance(color_images, dict) and color_images:
        keys = [_matching_color(color_images, c) for c in ([lead] if lead else []) + stocked]
        ordered: list[str] = []
        for key in keys:
            paths = color_images.get(key) if key else None
            if isinstance(paths, list) and paths and paths[0] not in ordered:
                ordered.append(paths[0])
        return ordered
    images = payload.get("images")
    return [p for p in images if isinstance(p, str) and p] if isinstance(images, list) else []


def _answered(history: list[dict], product_id: str) -> dict | None:
    """The newest `get_variants` answer for this product already in the
    conversation, or None.

    The same answer the tool-layer cache (`tools.base._cached_result`) would
    serve for it: re-reading a product the model has just looked up is a
    second database read for a photograph whose facts are already here.
    """
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if message.get("role") != TOOL_RESULTS:
            continue
        for result in message.get("results") or []:
            content = result.get("content")
            if (
                result.get("name") == "get_variants"
                and isinstance(content, dict)
                and content.get("product_id") == product_id
                and "variants" in content
            ):
                return content
    return None


def _garment_photos(ctx: ToolContext) -> int:
    return sum(1 for path in ctx.attachments if path in ctx.photo_products)


def _every_photo(payload: dict) -> set[str]:
    """All of a product's photographs, sold-out colourways included -- what
    "has this product been shown before" is checked against."""
    found = {p for p in payload.get("images") or [] if isinstance(p, str)}
    for paths in (payload.get("color_images") or {}).values():
        found.update(p for p in paths or [] if isinstance(p, str))
    return found


def show(ctx: ToolContext, text: str, history: list[dict], called: list[str]) -> list[str]:
    """Attach the photographs this reply's words call for. Returns what it added.

    Called once, on the reply that is about to leave, before
    `photo_claims.unbacked_claim` reads it -- so a reply that says «دي صورة
    WANAS Hoodie» carries one even when the model skipped the tool call, and
    the claim check sees the attachments as they will actually go.
    """
    if not text or any(name in _NOT_MERCHANDISE for name in called):
        return []
    if "get_size_chart" in called and not {"get_products", "get_variants"} & set(called):
        # A sizing answer: the chart is the picture it owes, and a gallery of
        # the garment beside a measurements table buries the table.
        return []

    added: list[str] = []
    payloads: dict[str, dict] = {}
    leads: dict[str, str | None] = {}

    def _payload(product_id: str) -> dict | None:
        if product_id not in payloads:
            payloads[product_id] = _answered(history, product_id) or catalog.get_variants(
                ctx.session, product_id
            )
        return payloads[product_id]

    for product_id, _name, start, end in named_products(text, known_products(history))[
        :MAX_SHOWCASE_PRODUCTS
    ]:
        payload = _payload(product_id)
        if payload is None:
            continue  # archived since it was shown: never offered again
        stocked = _in_stock_colours(payload)
        if not payload.get("in_stock"):
            continue  # nothing of it can be bought; a photo is an invitation
        colour = colour_named(
            _clause_around(text, start, end), list(payload.get("color_images") or {})
        )
        leads[product_id] = colour if colour in stocked else None
        if ctx.photos_of(product_id):
            continue  # the tool layer already attached this product's photo
        if _garment_photos(ctx) >= MAX_SHOWCASE_PHOTOS:
            break
        candidates = _photos(payload, leads[product_id], stocked)
        if _every_photo(payload) & ctx.sent_images:
            # Shown before in this conversation. The same rule the tool layer
            # keeps: a product already seen is not shown again in another
            # colour nobody asked for -- only the colour this reply names.
            candidates = candidates[:1] if leads[product_id] else []
        labels = _image_labels(payload, product_id)
        for path in candidates:
            if path in ctx.sent_images:
                continue
            if ctx.attach(path, label=labels.get(path), product=product_id):
                added.append(path)
                break

    shown = {ctx.photo_products[path] for path in ctx.attachments if path in ctx.photo_products}
    # «دي صورتهم الاتنين» is a sentence about two things. Topping up one
    # product with its other colourways would make two pictures of one shirt
    # look like the "both" it promised -- and `photo_claims` counts pictures.
    if len(shown) == 1 and not _BOTH.search(text):
        (product_id,) = shown
        payload = _payload(product_id) if product_id else None
        if payload is not None and product_id not in ctx.gallery:
            candidates = _photos(payload, leads.get(product_id), _in_stock_colours(payload))
            if not _every_photo(payload) & ctx.sent_images:
                # The first time this product is shown at all, and it is the
                # only one in the reply: its other colourways too, unseen and
                # in stock only, up to the first-showing budget.
                ctx.allow_gallery(product_id, FIRST_SHOWING_PHOTOS)
                labels = _image_labels(payload, product_id)
                for path in candidates:
                    if ctx.photos_of(product_id) >= FIRST_SHOWING_PHOTOS:
                        break
                    if path not in ctx.attachments and ctx.attach(
                        path, label=labels.get(path), product=product_id
                    ):
                        added.append(path)

    if added:
        log.info("showcase attached %d photo(s) for %s", len(added), ", ".join(sorted(shown)))
    return added


#: Asking to see the garment, in two strengths.
#:
#: A photo *noun* («صور», «صورة», «photo») names what is wanted, so it counts
#: -- unless what follows it is the chart: «ابعتلي صورة جدول المقاسات» is a
#: request for the chart (`_ABOUT_THE_CHART`).
#:
#: A *verb* of seeing («اشوف», «وريني») says only "show me", and the object
#: decides what. In a message about sizing it is the chart they want to see:
#: production's «عايز اشوف السايز شارت بتاع boxy wns tee» was read as a
#: request for photos, and the first version of this rule knew «جدول / مقاس /
#: قياس» but not the loanwords «السايز شارت», so the garment went out beside
#: the chart. A verb therefore counts only when the message is not about
#: sizing at all.
_PHOTO_NOUN = re.compile(r"صور\w*|\b(?:photos?|pics?|pictures?|images?)\b", re.IGNORECASE)
_SEE_VERB = re.compile(
    r"وريني\w*|وريهولي|فرجني\w*|شكله|شكلها|شكلهم|أشوف|اشوف|نشوف|\b(?:see|show)\b",
    re.IGNORECASE,
)
#: What the chart is called, in every spelling the customers use.
_CHART_WORDS = (
    r"جدول|مقاس|قياس|سايز|سيز|شارت|تشارت|size|chart|measurement|sizing"
)
_ABOUT_THE_CHART = re.compile(
    # «ال» / «بال» / «لل» may lead the chart word; «و» may not -- «صور
    # التيشيرت والجدول» asks for both, not for a picture of the chart.
    rf"\s*(?:\S+\s+){{0,2}}?(?:ال|بال|لل|ل|ب)?(?:{_CHART_WORDS})", re.IGNORECASE
)


def asked_for_photos(ctx: ToolContext) -> bool:
    """Did the customer's own last message ask to see the garment itself?

    Read from what they wrote, like `catalog_tools.asked_about_sizing` -- the
    two together decide whether a reply may carry garment photos at all. What
    they wrote, not what the runtime wrote beside it: the note about a photo
    *they* sent begins «[الزبون بعت صورة]», and its «صورة» was read as a
    request for ours (`assistant/customer_words.py`).
    """
    from assistant.tools.catalog_tools import asked_about_sizing

    text = customer_words.latest(ctx.history)
    for match in _PHOTO_NOUN.finditer(text):
        if not _ABOUT_THE_CHART.match(text, match.end()):
            return True
    if asked_about_sizing(ctx):
        return False
    return bool(_SEE_VERB.search(text)) or asked_for_colors(ctx)


def _ensure_chart(ctx: ToolContext, text: str) -> str | None:
    """Attach the chart a sizing question is owed, when nothing attached one.

    The product is the one the reply names, if it names exactly one, else the
    one the conversation is about (`tools.base.last_product`) -- never a
    guess between two. Forced past "already sent": asking for the chart
    again is asking to see it again, the same rule `get_size_chart` keeps.
    """
    from assistant.tools.catalog_tools import _chart_image

    if any(path not in ctx.photo_products for path in ctx.attachments):
        return None  # a chart is already going
    named = {pid for pid, *_ in named_products(text or "", known_products(ctx.history))}
    if len(named) == 1:
        (product_id,) = named
    elif not named:
        current = last_product(ctx.history)
        product_id = (current or {}).get("product_id")
    else:
        return None
    if not product_id:
        return None
    chart = _chart_image(ctx.session, product_id)
    if not chart:
        return None
    name = next((n for n, pid in known_products(ctx.history).items() if pid == product_id), None)
    if ctx.attach(
        chart, force=True, chart=True, label=_chart_label({"name": name or product_id}, product_id)
    ):
        log.info("attached the %s size chart the sizing question was owed", product_id)
        return chart
    return None


def keep_chart_or_photos(ctx: ToolContext, called=(), text: str = "") -> list[str]:
    """A size-chart question gets no garment photo; a product question gets no
    chart. Returns what it took off.

    Reported from production as «when I ask for the size chart it sends the
    chart together with product photos», and it did, by design:
    `get_variants` attached the product's photo on every call and the chart
    beside it whenever the message was about sizing, a single-hit
    `get_products` did the same, and the showcase could then add colourways
    on top. A measurements table between pictures of a T-shirt is a table
    nobody can find.

    Decided here, once, on the pictures that are actually leaving -- after
    every tool call and the showcase -- rather than inside each tool, because
    three doors lead to the mix and a rule kept at each door is a rule one
    new door forgets. From the customer's own words:

    * about sizing and not asking for photos -> **no garment photo**, whether
      or not a chart picture exists to send instead: «a size chart question
      must never get a product photo»;
    * asking for photos and not about sizing -> no chart;
    * neither, with both attached -> the chart alone (it only attaches for a
      sizing question or an explicit `get_size_chart`, so it is the answer);
    * asked for both («ابعتلي صوره وجدول المقاسات») -> both.

    What is taken off is taken off entirely, so it is never recorded as
    delivered: the photo a chart reply withheld is still unseen when the
    customer asks for it next.

    Two more facts decide it besides the words, both from production on
    2026-09-25. A turn that called `get_size_chart` is a sizing turn, whatever
    the customer's phrasing. And a sizing question gets the chart even when
    the model answered it from memory: «طيب السايز شارت بتاع ringer boxy
    fit» was answered with the numbers and no tool call, and what went out
    was three photos of the shirt and no chart (`_ensure_chart`).
    """
    from assistant.tools.catalog_tools import asked_about_sizing

    wants_photos = asked_for_photos(ctx)
    asked_sizing = asked_about_sizing(ctx)
    wants_chart = asked_sizing or "get_size_chart" in (called or ())
    if asked_sizing and not wants_photos:
        _ensure_chart(ctx, text)

    photos = [path for path in ctx.attachments if path in ctx.photo_products]
    charts = [path for path in ctx.attachments if path not in ctx.photo_products]
    if wants_photos and wants_chart:
        return []
    if wants_chart:
        dropped = photos
    elif wants_photos:
        dropped = charts
    else:
        dropped = photos if charts else []
    for path in dropped:
        ctx.attachments.remove(path)
        ctx.attachment_labels.pop(path, None)
        ctx.photo_products.pop(path, None)
    if dropped:
        log.info(
            "withheld %d %s: the customer asked %s",
            len(dropped),
            "size chart(s)" if dropped is charts else "product photo(s)",
            "to see the garment" if dropped is charts else "about sizing, not for photos",
        )
    return dropped


def sent_pictures(outcomes: list, attachment_labels: dict | None = None) -> str:
    """One log line's worth: every picture a reply sent, as chart or photo,
    with what it showed and whether the platform took it.

    Production had no record of which pictures actually left: "the size
    chart came with product photos" could be read only off a customer's
    screenshot. `wanas.showcase: sent ...` is that record, for every reply
    that carries a picture.
    """
    parts: list[str] = []
    for out in outcomes:
        path = getattr(out, "image_path", None)
        if not path:
            continue
        label = (attachment_labels or {}).get(path) or {}
        wording = label.get("label") if isinstance(label, dict) else str(label)
        kind = "chart" if str(wording or "").endswith("size chart") else "photo"
        state = "ok" if getattr(out, "delivered", False) else "REFUSED"
        parts.append(f"{kind}[{wording or path.rsplit('/', 1)[-1]}]={state}")
    return ", ".join(parts)
