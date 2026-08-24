"""Catalog and sizing tools: get_categories, get_products, get_variants,
get_size_chart, get_shipping_fee, ask_governorate."""

from __future__ import annotations

from backend.models import Product
from backend.services import catalog, runtime_flags, shipping
from backend.services.size_charts import MEASUREMENT_NOTE, get_chart
from chatbot import interactive
from chatbot.tools.base import ToolContext, tool
from common.money import money
from config.settings import settings


@tool(
    "get_categories",
    "List what the shop actually sells: the product categories with counts, plus the style and "
    "department filters and the two optional collections. Use it to ground yourself in what exists "
    "before offering anything, when a request is too broad to search on. It tells you what to ask "
    "about -- it is not a menu to read back to the customer.",
)
def get_categories(ctx: ToolContext) -> dict:
    return catalog.get_categories(ctx.session)


@tool(
    "get_products",
    "Find products by category, style, department, collection, or free text. `query` is matched "
    "against product name, category, style and variant colours together, so a phrase like "
    "'olive hoodie' resolves even though colour is not part of any product name. All arguments "
    "are optional; with none it returns the whole catalog. price_from/price_to are the real "
    "min and max of that product's variant prices -- quote 'from X' when they differ. "
    "`colors` lists every colourway the product comes in including sold-out ones -- it describes "
    "the product, it is not an offer; `in_stock_colors` is the only list you may present as "
    "available. Never deny a colour that is in `in_stock_colors`, and never offer one that is not. "
    "Search for what the customer actually asked for, not for a product name you happen to know. "
    "The result is what you may choose from, not what you should list: for a vague request, offer "
    "two or three that fit and let them narrow it down.",
    properties={
        "category": {"type": "string", "description": "One of the categories from get_categories."},
        "style": {"type": "string", "description": "A style facet, e.g. oversized, zip-through."},
        "department": {"type": "string", "description": "unisex or women."},
        "collection": {"type": "string", "description": "Optional; most products have none."},
        "query": {"type": "string", "description": "Free text, any language or spelling."},
    },
)
def get_products(
    ctx: ToolContext,
    category: str | None = None,
    style: str | None = None,
    department: str | None = None,
    collection: str | None = None,
    query: str | None = None,
) -> dict:
    return catalog.get_products(
        ctx.session,
        category=category,
        style=style,
        department=department,
        collection=collection,
        query=query,
    )


@tool(
    "get_variants",
    "Every variant of one product with its variant_id, price and availability. You must call this "
    "before adding anything to a cart -- a variant_id cannot be guessed or constructed. Sold-out "
    "variants are returned too so you can say which combinations exist; `in_stock` is the only "
    "list you may offer from. If you already called this for the same product earlier in this "
    "conversation, its answer is still valid -- re-read it from what was already said instead of "
    "calling again, unless the customer is asking about a different product. Calling this attaches "
    "one product photo to your reply automatically (never more, and never one already sent), so "
    "describe the product in words and never mention a file path or a link. Only pass "
    "more_images=true when the customer explicitly asks to see more photos of this exact product; "
    "it then sends up to two more, still never repeating a photo already sent unless there is "
    "nothing else left to show.",
    properties={
        "product_id": {"type": "string", "description": "From get_products."},
        "more_images": {
            "type": "boolean",
            "description": "true only for an explicit 'show me more photos / other colours / other "
            "angles' request about a product already shown. Defaults to false.",
        },
    },
    required=("product_id",),
)
def get_variants(ctx: ToolContext, product_id: str, more_images: bool = False) -> dict:
    payload = catalog.get_variants(ctx.session, product_id)
    if payload is None:
        return {"error": "product_not_found", "product_id": product_id}
    if more_images:
        payload["_more_images"] = True
    return payload


@tool(
    "get_size_chart",
    "The published measurements for one product, plus the chart image, which the runtime attaches "
    "to your reply automatically. If it returns has_chart false there is no chart for that product: "
    "say so. Never estimate a measurement and never quote another product's chart.",
    properties={"product_id": {"type": "string"}},
    required=("product_id",),
)
def get_size_chart(ctx: ToolContext, product_id: str) -> dict:
    product = ctx.session.get(Product, product_id)
    if product is None:
        return {"error": "product_not_found", "product_id": product_id}

    chart = get_chart(product.size_chart)
    if chart is None:
        # Nothing else. Returning a neighbouring product's chart is the failure
        # this shape exists to prevent -- a near-enough chart produces
        # confident, precise, wrong numbers, and sizing wrong causes a return.
        return {"has_chart": False, "product_id": product_id}

    return {
        "has_chart": True,
        "chart_id": chart["chart_id"],
        "title": chart["title"],
        "unit": chart.get("unit", "cm"),
        # Supplied by the tool, not stored per chart, so every chart answers
        # the same shape and the garment-flat caveat can never be missing.
        "measurement_note": MEASUREMENT_NOTE,
        "length_specific": bool(chart.get("length_specific", False)),
        "measurements": chart["measurements"],
        "sizes": chart["sizes"],
        "image": chart.get("image"),
    }


@tool(
    "get_shipping_fee",
    "The delivery fee for a governorate. The governorate is a picked value from a fixed list, not "
    "free text, because it sets the price. Call this while collecting the address so the summary "
    "shows a real total.",
    properties={"governorate": {"type": "string", "description": "English or Arabic name."}},
    required=("governorate",),
)
def get_shipping_fee(ctx: ToolContext, governorate: str) -> dict:
    resolved = shipping.resolve(ctx.session, governorate)
    if resolved is None:
        return {
            "error": "unknown_governorate",
            "valid": shipping.valid_governorates(ctx.session),
        }
    fee = shipping.get_fee(ctx.session, resolved)
    if fee is None:
        # The shop has not priced it. An order for it cannot be confirmed --
        # shipping free by accident is a real loss on every parcel.
        return {"error": "no_rate_set", "governorate": resolved}
    return {"governorate": resolved, "fee": money(fee)}


def _interactive_enabled(ctx: ToolContext) -> bool:
    return runtime_flags.get(
        ctx.session, "interactive_messages_enabled", settings.interactive_messages_enabled
    )


@tool(
    "ask_governorate",
    "Ask which governorate to ship to, as a list the customer taps rather than a question they "
    "answer in prose. Call it with no arguments to offer the six regions; when they pick one, call "
    "it again with that region to offer its governorates. The governorate sets the shipping fee, so "
    "it has to be one of the twenty-seven real values -- this is what makes that true instead of "
    "hoping the address text can be parsed. Ask for the street address separately, in words. If the "
    "customer simply names their governorate themselves, you do not need this at all: go straight "
    "to get_shipping_fee.",
    properties={
        "region": {
            "type": "string",
            "description": "The region the customer just picked. Omit on the first call.",
        }
    },
)
def ask_governorate(ctx: ToolContext, region: str | None = None) -> dict:
    if not region:
        regions = shipping.regions()
        payload = {
            "step": "region",
            "regions": [
                {"region_id": item["region_id"], "label_ar": item["label_ar"]} for item in regions
            ],
        }
        if _interactive_enabled(ctx):
            payload["picker_sent"] = ctx.offer(interactive.region_picker(regions))
        return payload

    resolved = shipping.resolve_region(region)
    if resolved is None:
        # Not a region. It is very often a governorate the customer typed
        # straight out, so say which it was rather than making them start over.
        governorate = shipping.resolve(ctx.session, region)
        if governorate is not None:
            return {"step": "done", "governorate": governorate}
        return {
            "error": "unknown_region",
            "regions": [item["region_id"] for item in shipping.regions()],
        }

    rows = shipping.governorates_in_region(ctx.session, resolved)
    if not rows:
        return {"error": "unknown_region", "regions": [item["region_id"] for item in shipping.regions()]}

    label = next(
        (item["label_ar"] for item in shipping.regions() if item["region_id"] == resolved), resolved
    )
    payload = {
        "step": "governorate",
        "region": resolved,
        "governorates": [
            {"governorate": row["governorate"], "label_ar": row["label_ar"]} for row in rows
        ],
    }
    if _interactive_enabled(ctx):
        payload["picker_sent"] = ctx.offer(interactive.governorate_picker(rows, label))
    return payload
