"""Providers that need no API key.

Two of them, and both exist for the same reason: the LLM key is one of the
things AGENTS.md says will arrive later, and none of it may block progress.

* `ScriptedProvider` replays a fixed list of replies. The tests use it to
  drive the real agent loop, the real session storage and the real tools with
  no network and no non-determinism.

* `RehearsalProvider` is the harness stand-in. It maps typed commands to tool
  calls and renders tool results back into text, so the whole flow -- browse,
  variants, cart, sizing, shipping, order, modify, cancel, feedback, handoff --
  can be walked end to end before a key exists.

  It is deliberately a dumb command mapper. It is **not** the product and must
  never become the fallback in production: the entire argument for an LLM agent
  is that keyword matching cannot read "3ayez el hoodie" or "الهودي الزيتي".
  Set LLM_PROVIDER=gemini and a key to get the real thing.
"""

from __future__ import annotations

import re

from assistant.messages import TOOL_RESULTS, USER
from assistant.providers.base import CommentClassification, ImageReading, LLMProvider, ModelReply


class ScriptedProvider(LLMProvider):
    name = "scripted"

    #: Media is scripted the same way replies are, so a test can drive the
    #: voice-note and photo paths without a key and without a network call.
    supports_audio = True
    supports_vision = True

    def __init__(self, script: list[ModelReply] | None = None):
        self.script = list(script or [])
        self.calls: list[tuple[str, list[dict], list]] = []
        self.transcripts: list[str] = []
        self.readings: list[ImageReading] = []
        self.classifications: list[CommentClassification] = []
        #: What was actually handed to the media calls, for assertions.
        self.audio_calls: list[tuple[bytes, str]] = []
        self.image_calls: list[tuple[bytes, str, list[dict]]] = []
        self.comment_calls: list[str] = []

    def push(self, reply: ModelReply) -> None:
        self.script.append(reply)

    def push_transcript(self, text: str) -> None:
        self.transcripts.append(text)

    def push_reading(self, reading: ImageReading) -> None:
        self.readings.append(reading)

    def push_classification(self, category: str) -> None:
        self.classifications.append(CommentClassification(category=category))

    def generate(self, system_prompt: str, history: list[dict], tools: list) -> ModelReply:
        self.calls.append((system_prompt, list(history), tools))
        if not self.script:
            return ModelReply(text="(scripted provider exhausted)")
        return self.script.pop(0)

    def transcribe(self, audio: bytes, mime_type: str, *, hint: str = "") -> str:
        self.audio_calls.append((audio, mime_type))
        return self.transcripts.pop(0) if self.transcripts else ""

    def inspect_image(self, image: bytes, mime_type: str, *, catalog: list[dict]) -> ImageReading:
        self.image_calls.append((image, mime_type, list(catalog)))
        return self.readings.pop(0) if self.readings else ImageReading()

    def classify_comment(self, text: str) -> CommentClassification:
        self.comment_calls.append(text)
        return self.classifications.pop(0) if self.classifications else CommentClassification()


HELP = (
    "أهلاً 👋 (rehearsal mode — no LLM key set, so type commands)\n"
    "  categories                      | الأقسام\n"
    "  products [text]                 | products hoodie\n"
    "  variants <product_id>\n"
    "  size <product_id>\n"
    "  add <variant_id> [qty]\n"
    "  cart | remove <variant_id> | clear\n"
    "  ship <governorate>\n"
    "  gov [region]                    | the tappable governorate picker\n"
    "  profile | link yes | link no\n"
    "  order <name> | <governorate> | <address> | <phone>\n"
    "  orders [all] | qty <order_id> <variant_id> <n> | cancel <order_id>\n"
    "  swap <order_id> <variant_id> [note] | rate <order_id> <1-5> [text]\n"
    "  human <reason> <summary>\n"
)


class RehearsalProvider(LLMProvider):
    """A stand-in, not a classifier design. See the module docstring."""

    name = "rehearsal"

    # No key, so no transcription and no vision. Declaring that here is what
    # makes the runtime fall back to a person for a voice note instead of
    # silently dropping it -- the same behaviour as before this feature, which
    # is the correct thing for a stand-in to do.
    supports_audio = False
    supports_vision = False

    def generate(self, system_prompt: str, history: list[dict], tools: list) -> ModelReply:
        last = history[-1] if history else None
        if last is None:
            return ModelReply(text=HELP)
        if last.get("role") == TOOL_RESULTS:
            return ModelReply(text=self._render(last["results"]))
        if last.get("role") != USER:
            return ModelReply(text="…")
        return self._plan(last.get("content", "").strip())

    # -- planning ---------------------------------------------------------

    def _plan(self, text: str) -> ModelReply:
        lowered = text.lower()

        def call(name: str, arguments: dict | None = None) -> ModelReply:
            return ModelReply(tool_calls=[{"id": "c1", "name": name, "arguments": arguments or {}}])

        if lowered in {"help", "?", "مساعدة"}:
            return ModelReply(text=HELP)
        if lowered in {"categories", "cats", "الاقسام", "الأقسام"}:
            return call("get_categories")
        if lowered == "cart":
            return call("view_cart")
        if lowered == "clear":
            return call("remove_from_cart", {"clear_all": True})
        if lowered == "profile":
            return call("get_my_profile")
        if lowered in {"link yes", "link y"}:
            return call("link_client", {"confirmed": True})
        if lowered in {"link no", "link n"}:
            return call("link_client", {"confirmed": False})
        if lowered in {"orders", "orders all"}:
            return call("get_my_orders", {"include_closed": lowered.endswith("all")})

        if match := re.match(r"^products?\s*(.*)$", lowered):
            query = match.group(1).strip()
            return call("get_products", {"query": query} if query else {})
        if match := re.match(r"^variants?\s+(\S+)$", lowered):
            return call("get_variants", {"product_id": match.group(1)})
        if match := re.match(r"^size\s+(\S+)$", lowered):
            return call("get_size_chart", {"product_id": match.group(1)})
        if match := re.match(r"^add\s+(\S+)(?:\s+(\d+))?$", lowered):
            args = {"variant_id": match.group(1)}
            if match.group(2):
                args["quantity"] = int(match.group(2))
            return call("add_to_cart", args)
        if match := re.match(r"^remove\s+(\S+)$", lowered):
            return call("remove_from_cart", {"variant_id": match.group(1)})
        if match := re.match(r"^ship\s+(.+)$", text, re.IGNORECASE):
            return call("get_shipping_fee", {"governorate": match.group(1).strip()})
        if match := re.match(r"^gov\s*(.*)$", text, re.IGNORECASE):
            # Walks the two-step picker so the harness can see the list message
            # without a Meta credential.
            region = match.group(1).strip()
            return call("ask_governorate", {"region": region} if region else {})
        if match := re.match(r"^order\s+(.+)$", text, re.IGNORECASE):
            parts = [p.strip() for p in match.group(1).split("|")]
            if len(parts) != 4:
                return ModelReply(text="order <name> | <governorate> | <address> | <phone>")
            return call(
                "confirm_order",
                {
                    "customer_name": parts[0],
                    "governorate": parts[1],
                    "address": parts[2],
                    "contact_phone": parts[3],
                },
            )
        if match := re.match(r"^qty\s+(\S+)\s+(\S+)\s+(\d+)$", lowered):
            return call(
                "modify_order_quantity",
                {
                    "order_id": match.group(1).upper(),
                    "variant_id": match.group(2),
                    "quantity": int(match.group(3)),
                },
            )
        if match := re.match(r"^cancel\s+(\S+)$", lowered):
            return call("cancel_order", {"order_id": match.group(1).upper()})
        if match := re.match(r"^swap\s+(\S+)\s+(\S+)(?:\s+(.*))?$", text, re.IGNORECASE):
            return call(
                "request_item_swap",
                {
                    "order_id": match.group(1).upper(),
                    "from_variant_id": match.group(2),
                    "note": (match.group(3) or "").strip() or None,
                },
            )
        if match := re.match(r"^rate\s+(\S+)\s+(\d)(?:\s+(.*))?$", text, re.IGNORECASE):
            args = {"order_id": match.group(1).upper(), "rating": int(match.group(2))}
            if match.group(3):
                args["text"] = match.group(3).strip()
            return call("submit_feedback", args)
        if match := re.match(r"^human\s+(\S+)\s*(.*)$", text, re.IGNORECASE):
            return call(
                "request_human",
                {"reason": match.group(1).lower(), "summary": match.group(2) or "customer asked"},
            )

        return ModelReply(text=HELP)

    # -- rendering --------------------------------------------------------

    def _render(self, results: list[dict]) -> str:
        return "\n".join(self._render_one(r["name"], r["content"]) for r in results)

    def _render_one(self, name: str, content: dict) -> str:
        if "error" in content:
            detail = {k: v for k, v in content.items() if k != "error"}
            return f"[{content['error']}] {detail}" if detail else f"[{content['error']}]"

        if name == "get_categories":
            cats = ", ".join(f"{c['category']} ({c['product_count']})" for c in content["categories"])
            return f"الأقسام: {cats}\ncollections (optional): {', '.join(content['collections'])}"

        if name == "get_products":
            if not content["products"]:
                return "مفيش حاجة بالوصف ده."
            lines = []
            for p in content["products"]:
                price = (
                    f"{p['price_from']}"
                    if p["price_from"] == p["price_to"]
                    else f"من {p['price_from']} لـ {p['price_to']}"
                )
                stock = "" if p["any_in_stock"] else " — sold out"
                lines.append(f"• {p['name']} [{p['product_id']}] {price} EGP{stock}")
            return "\n".join(lines)

        if name == "get_variants":
            lines = [f"{content['name']}:"]
            for v in content["variants"]:
                bits = ", ".join(b for b in (v["color"], v["size"], v["length"]) if b)
                lines.append(f"  {v['variant_id']} — {bits} — {v['price']} EGP — {v['status']}")
            return "\n".join(lines)

        if name == "ask_governorate":
            if content.get("step") == "region":
                names = ", ".join(
                    f"{r['label_ar']} [{r['region_id']}]" for r in content["regions"]
                )
                return f"اختار المنطقة: {names}"
            if content.get("step") == "governorate":
                names = ", ".join(
                    f"{g['label_ar']} [{g['governorate']}]" for g in content["governorates"]
                )
                return f"محافظات {content['region']}: {names}"
            return f"المحافظة: {content.get('governorate')}"

        if name == "get_size_chart":
            if not content.get("has_chart"):
                return "مفيش chart منشور للمنتج ده."
            rows = "\n".join(f"  {size}: {vals}" for size, vals in content["sizes"].items())
            note = content["measurement_note"]
            extra = " (اسأل Long ولا Short الأول)" if content["length_specific"] else ""
            return f"{content['title']} ({content['unit']}) — {note}{extra}\n{rows}"

        if name in {"add_to_cart", "view_cart", "remove_from_cart"}:
            if not content["lines"]:
                return "الشنطة فاضية."
            lines = [
                f"  {ln['quantity']}× {ln['product_name']} — "
                f"{', '.join(b for b in (ln['color'], ln['size'], ln['length']) if b)} — "
                f"{ln['line_total']} EGP"
                for ln in content["lines"]
            ]
            return "الشنطة:\n" + "\n".join(lines) + f"\nالمجموع: {content['subtotal']} EGP"

        if name == "get_shipping_fee":
            return f"الشحن لـ {content['governorate']}: {content['fee']} EGP"

        if name == "confirm_order":
            items = "\n".join(
                f"  {i['quantity']}× {i['product_name']} — "
                f"{', '.join(b for b in (i['color'], i['size'], i['length']) if b)}"
                for i in content["items"]
            )
            return (
                f"تم ✅ رقم الطلب {content['order_id']}\n{items}\n"
                f"المنتجات: {content['subtotal']} + الشحن: {content['shipping_fee']} "
                f"= {content['total']} EGP (كاش عند الاستلام)"
            )

        if name == "get_my_orders":
            if not content["orders"]:
                return "مفيش طلبات مفتوحة."
            return "\n".join(
                f"  {o['order_id']} — {o['status']} — {o['total']} EGP — "
                f"{'قابل للتعديل' if o['modifiable'] else 'مش قابل للتعديل'}"
                for o in content["orders"]
            )

        if name == "modify_order_quantity":
            return f"اتعدل. الإجمالي الجديد {content['total']} EGP."
        if name == "cancel_order":
            return f"{content['order_id']} اتلغى."
        if name == "request_item_swap":
            return f"طلب التبديل وصل ({content['request_id']}) — حد من الفريق هيأكدلك."
        if name == "submit_feedback":
            return "تسلم! التقييم اتسجل."
        if name == "request_human":
            return "هحولك لحد من الفريق، استنى شوية."
        if name == "get_my_profile":
            if not content.get("known"):
                pending = content.get("pending_link")
                if pending:
                    return (
                        f"هو ده انت؟ {pending['masked_name']} ({pending['matched_on']}) "
                        f" — رد بـ 'link yes' أو 'link no'"
                    )
                return "لسه معرفكش — أول مرة."
            return f"{content['full_name']} — {content['governorate']} — {content['address']}"
        if name == "link_client":
            return "تمام، ربطت الحساب." if content.get("linked") else "تمام، هفضل أعاملك كعميل جديد."

        return str(content)
