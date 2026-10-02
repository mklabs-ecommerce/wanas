"""Model benchmark: a fixed scenario suite run end-to-end through the real bot.

    python scripts/rehla/benchmark.py [--only NAME] [--label LABEL]

Same isolation as `demo_conversations.py` (imported for it): no Shopify, no
Meta, the local shelf on `rehla_demo.db`, outbound to the log sender. The LLM
is whatever `.env` configures -- this is the point: run it once per model and
compare. Every OpenRouter request is observed at the HTTP layer, so tokens,
cached tokens and cost include the media calls (photo, voice) as well as chat.

Writes `benchmarks/<model>_<date>.json` and `benchmarks/<model>_<date>.md`.

Each scenario's checks are listed in its `expect`. On top of those, every
reply is held to three suite-wide checks:

  money     every amount said with «جنيه» is a catalog price, a shipping fee,
            or a sum of up to three prices plus at most one fee
  dialect   no Modern Standard / Levantine / Gulf word an Egyptian shop
            assistant would not say
  arabic    the reply is mostly Arabic script (product names stay Latin)
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_conversations as demo  # noqa: E402  (blanks vendors, pins the demo db)

REPO = demo.REPO
FIXTURES = REPO / "benchmarks" / "fixtures"
PHOTO = str(FIXTURES / "customer_photo.jpg")
VOICE = str(FIXTURES / "voice_order.ogg")

ORDER_STEPS_CAIRO = [
    "عايزة Rehla Original Tops لون Black مقاس M واحدة",
    "بس كده، عايزة أأكد الأوردر",
    "اسمي منة أحمد",
    "القاهرة",
    "15 شارع التحرير، الدقي، الدور التالت شقة 5",
    "01012345678",
    "أيوه أكدي",
]

#: name, messages, expect. A message is text, or a dict {text, image, audio, gap_hours}.
SCENARIOS: list[tuple[str, list, dict]] = [
    ("greeting_browse", ["مساء الخير", "عايزة أتفرج على التوبات اللي عندكم"],
     {"tools": {"get_products"}, "reply_has": [r"Top"]}),
    ("arabic_search", ["عندكم توب كم طويل لونه بني مقاس L؟ بكام؟"],
     {"tools_any": {"get_products", "get_variants"}, "reply_has": [r"445"]}),
    ("image_match", [{"text": "عايزة ده", "image": PHOTO}],
     {"reply_has": [r"T-?Shirt|تيشيرت"], "not_tools": {"request_human"}}),
    ("voice_note", [{"audio": VOICE}],
     {"tools_any": {"get_products", "get_variants"}, "reply_has": [r"60\d|599"],
      "not_tools": {"request_human"}}),
    ("order_cairo_70", ORDER_STEPS_CAIRO,
     {"tools": {"confirm_order"}, "order": {"total": Decimal("515"), "shipping": Decimal("70"),
                                           "name": "منة أحمد"}}),
    ("order_alex_85", [
        "عايزة Rehla Jacket الأسود مقاس M",
        "تمام ضيفيه وأكدي الأوردر",
        "الاسم سارة محمود، إسكندرية، 20 شارع فؤاد محطة الرمل، 01123456789",
        "أيوه أكدي",
    ], {"tools": {"confirm_order"}, "order": {"total": Decimal("1085"), "shipping": Decimal("85"),
                                             "name": "سارة محمود"}}),
    ("exchange_return", ["لو المقاس طلع مش مظبوط أقدر أبدل أو أرجع؟"],
     {"reply_has": [r"استبدال|استرجاع|تبديل|ترجيع|مرتجع|تبدل|ترجع|نبدل"], "not_tools": {"add_to_cart"}}),
    ("size_help", ["البنطلون الواسع البني متاح مقاس M؟", "مش عارفة مقاسي، ممكن جدول المقاسات؟"],
     {"tools": {"request_human"}, "paused": True}),
    ("unsold_garment", ["عندكم فساتين سواريه؟"],
     {"lacks": [r"^أيوه", r"أيوه عندنا"], "not_tools": {"request_human", "add_to_cart"}}),
    ("drift_then_return", [
        "عايزة Rehla Original Tops لون Pink مقاس S",
        "ضيفيه",
        "بالمناسبة التوصيل بياخد كام يوم؟ وبتدفعوا إزاي؟",
        "طب تمام، كملي الأوردر. اسمي هند علي، الجيزة، 3 شارع الهرم، 01234567890",
        "أكدي",
    ], {"tools": {"confirm_order"}, "order": {"total": Decimal("515"), "shipping": Decimal("70"),
                                             "name": "هند علي"}}),
    ("multi_item_cart_edit", [
        "عايزة Rehla Jacket الأسود مقاس M وRehla White T-Shirt مقاس M",
        "وكمان Rehla Tops لون White مقاس S",
        "لا شيلي التيشيرت الأبيض",
        "وخلي التوب الأبيض قطعتين",
        "إيه اللي في الشنطة دلوقتي وبكام؟",
    ], {"tools": {"add_to_cart"}, "cart": {"lines": 2, "units": 3}, "reply_has": [r"1,?890"]}),
    ("angry_customer", ["إيه القرف ده!! الأوردر اتأخر أسبوع ومحدش بيرد عليا، أنا مش هتعامل معاكم تاني"],
     {"tools": {"request_human"}, "paused": True}),
    ("ambiguous_short", ["بكام؟", "متاح؟"],
     {"not_tools": {"add_to_cart", "confirm_order"}, "reply_has": [r"؟"]}),
    ("long_memory", [
        "السلام عليكم، أنا ريم حسن",
        "أنا ساكنة في المنصورة",
        "عندكم جواكت؟",
        "الجاكيت بكام؟",
        "وعندكم تيشيرتات بيضا؟",
        "طب عندكم كابات؟",
        "الكاب الأسود بكام؟",
        "وتوبات الكم الطويل؟",
        "الـ Flared ألوانه إيه؟",
        "مقاسات إيه؟",
        "التوصيل للمنصورة بكام؟",
        "تمام، عايزة الجاكيت مقاس M",
        "ضيفيه وأكدي الأوردر",
        "العنوان 12 شارع الجمهورية المنصورة، والرقم 01098765432، والاسم زي ما قلتلك",
        "أيوه أكدي",
    ], {"tools": {"confirm_order"}, "order": {"total": Decimal("1085"), "shipping": Decimal("85"),
                                             "name": "ريم حسن"}}),
]

#: Words that are not Egyptian shop Arabic (cf. assistant/reply_rules.py).
_NOT_EGYPTIAN = re.compile(r"(?<!\w)(وين|لدينا|تبعك|تبعي|شو|هلق|سوف|ماذا|لماذا|يمكنك|عزيزتي العميلة)(?!\w)")
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:جنيه|ج\.?م|EGP)")
_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")


class Meter:
    """Counts every OpenRouter call: tokens, cache hits, cost, seconds."""

    def __init__(self):
        self.calls: list[dict] = []

    def install(self):
        from assistant.providers import openrouter

        real = openrouter.httpx.post

        def post(url, *args, json=None, **kwargs):
            if isinstance(json, dict):
                json.setdefault("usage", {"include": True})
            started = time.perf_counter()
            response = real(url, *args, json=json, **kwargs)
            try:
                body = response.json()
            except ValueError:
                body = {}
            usage = body.get("usage") or {}
            self.calls.append({
                "model": (json or {}).get("model"),
                "status": response.status_code,
                "seconds": round(time.perf_counter() - started, 2),
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
                "cost": float(usage.get("cost") or 0),
            })
            return response

        openrouter.httpx.post = post

    def since(self, start: int) -> dict:
        calls = self.calls[start:]
        return {
            "calls": len(calls),
            "errors": sum(1 for c in calls if c["status"] >= 400),
            "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
            "completion_tokens": sum(c["completion_tokens"] for c in calls),
            "cached_tokens": sum(c["cached_tokens"] for c in calls),
            "cost_usd": round(sum(c["cost"] for c in calls), 6),
            "models": sorted({c["model"] or "?" for c in calls}),
        }


def _allowed_amounts() -> set[Decimal]:
    from domain.db import session_scope
    from domain.models import Variant
    from domain.services import shop_facts

    with session_scope() as db:
        prices = {Decimal(str(v.price)).quantize(Decimal("0.01")) for v in db.query(Variant).all() if v.price}
        # "was 699, now 450": the struck-through price is a catalog fact too.
        originals = {Decimal(str(v.original_price)).quantize(Decimal("0.01"))
                     for v in db.query(Variant).all() if v.original_price}
    fees = set(shop_facts.SHIPPING_FEES.values()) | {shop_facts.DEFAULT_SHIPPING_FEE}
    sums = set(prices)
    for n in (2, 3):
        sums |= {sum(c) for c in itertools.combinations_with_replacement(prices, n)}
    return sums | fees | originals | {s + f for s in sums for f in fees}


def _plain(text: str) -> str:
    """Without tashkeel: «تبدّلي» is «تبدلي» to a pattern."""
    return re.sub(r"[ً-ْـ]", "", text)


def _reply_problems(reply: str, allowed: set[Decimal]) -> list[str]:
    out = []
    for raw in _AMOUNT.findall(reply):
        amount = Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
        if amount not in allowed:
            out.append(f"unsupported amount {raw}")
    if m := _NOT_EGYPTIAN.search(reply):
        out.append(f"non-Egyptian word {m.group(0)!r}")
    arabic, latin = len(_ARABIC.findall(reply)), len(_LATIN.findall(reply))
    if reply and arabic * 4 < latin:  # colour/product names are Latin on purpose
        out.append("reply mostly not Arabic")
    return out


def run(only: str | None, label: str | None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    for suffix in ("", "-wal", "-shm"):
        Path(f"rehla_demo.db{suffix}").unlink(missing_ok=True)

    import logging

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    import app as app_module
    from assistant import session as session_store
    from assistant.runtime import handle_message
    from common import telemetry
    from config.settings import settings
    from domain.db import engine, session_scope
    from domain.models import Base, Order
    from domain.services import carts, identities, notifications

    assert not settings.shopify_configured and settings.local_store_active
    Base.metadata.create_all(engine)
    app_module._ensure_schema_columns()
    app_module._ensure_catalog_seeded()
    app_module._ensure_shipping_fees_set()
    notifications.register_transcript_recorder(app_module.assistant_runtime.record_outbound)

    meter = Meter()
    meter.install()
    allowed = _allowed_amounts()
    model = settings.llm_model or "default"
    media_model = ((settings.llm_media_model or "").strip() or "default") + (f" / audio {settings.llm_audio_model}" if settings.llm_audio_model else "")
    label = label or model.replace("/", "_")
    print(f"model={model} media={media_model}\n")

    results = []
    for index, (name, messages, expect) in enumerate(SCENARIOS, start=1):
        if only and only != name:
            continue
        external_id = f"2019000000{index:02d}"
        with session_scope() as db:
            session_store.clear(db, "whatsapp", external_id)
            carts.clear(db, "whatsapp", external_id) if hasattr(carts, "clear") else None
        print("=" * 70, f"\n{name}\n" + "=" * 70)
        start_calls = len(meter.calls)
        tools: set[str] = set()
        turns, problems = [], []
        paused = False
        started = time.perf_counter()
        for item in messages:
            item = item if isinstance(item, dict) else {"text": item}
            text = item.get("text", "")
            print(f"  زبون  › {text or ''}{' [photo]' if item.get('image') else ''}"
                  f"{' [voice]' if item.get('audio') else ''}")
            t0 = time.perf_counter()
            try:
                # Inside a telemetry turn, as the channel worker runs it: that
                # is where the provider reads its prompt-cache key from.
                with telemetry.turn("whatsapp", external_id):
                    reply = handle_message(
                        "whatsapp", external_id, text,
                        image_paths=[item["image"]] if item.get("image") else None,
                        audio_paths=[item["audio"]] if item.get("audio") else None,
                    )
            except Exception as exc:  # a crash is a failure, not the end of the run
                problems.append(f"crash: {exc!r}"[:200])
                break
            seconds = round(time.perf_counter() - t0, 2)
            sender = notifications.get_sender("whatsapp")
            proactive = [m.text for m in list(getattr(sender, "sent", []))]
            if hasattr(sender, "clear"):
                sender.clear()
            out = reply.text or ""
            paused = paused or bool(reply.paused)
            tools.update(reply.tool_calls or [])
            turns.append({"customer": text, "reply": out, "proactive": proactive, "seconds": seconds,
                          "tools": reply.tool_calls or [], "attachments": len(reply.attachments),
                          "transcript": reply.transcript})
            print("  بوت   › " + (out or "(silent)").replace("\n", "\n          "))
            if reply.tool_calls:
                print(f"          · tools: {', '.join(reply.tool_calls)}  ({seconds}s)")
            for p in _reply_problems(out, allowed):
                problems.append(f"turn {len(turns)}: {p}")

        replies = [t["reply"] for t in turns]
        problems += [f"tool {t} not called" for t in expect.get("tools", set()) if t not in tools]
        if expect.get("tools_any") and not (expect["tools_any"] & tools):
            problems.append(f"none of {sorted(expect['tools_any'])} called")
        problems += [f"tool {t} called" for t in expect.get("not_tools", set()) if t in tools]
        for pattern in expect.get("lacks", []):
            if any(re.search(pattern, r, re.M) for r in replies):
                problems.append(f"a reply matched {pattern!r}")
        for pattern in expect.get("reply_has", []):
            said = replies + [p for t in turns for p in t["proactive"]]
            if not any(re.search(pattern, _plain(r)) for r in said):
                problems.append(f"no reply matched {pattern!r}")
        with session_scope() as db:
            identity = identities.get(db, "whatsapp", external_id)
            paused = paused or bool(identity and identity.paused_until_staff_reply)
        if expect.get("paused") and not paused:
            problems.append("not handed to a person")
        if "cart" in expect:
            with session_scope() as db:
                lines = carts.cart_payload(db, "whatsapp", external_id).get("lines") or []
            units = sum(int(line.get("quantity") or line.get("qty") or 0) for line in lines)
            if len(lines) != expect["cart"]["lines"] or units != expect["cart"]["units"]:
                problems.append(f"cart has {len(lines)} line(s) / {units} unit(s)")
        order_info = None
        if "order" in expect:
            want = expect["order"]
            with session_scope() as db:
                order = (db.query(Order).filter(Order.source_external_id == external_id)
                         .order_by(Order.placed_at.desc()).first())
                if order is None:
                    problems.append("no order created")
                else:
                    order_info = {"total": str(order.total), "shipping": str(order.shipping_fee),
                                  "name": getattr(order.client, "full_name", None) if hasattr(order, "client") else None}
                    if Decimal(str(order.total)) != want["total"]:
                        problems.append(f"order total {order.total} != {want['total']}")
                    if Decimal(str(order.shipping_fee)) != want["shipping"]:
                        problems.append(f"shipping {order.shipping_fee} != {want['shipping']}")
                    if order_info["name"] and want["name"] not in order_info["name"]:
                        problems.append(f"order name {order_info['name']!r}")
        usage = meter.since(start_calls)
        result = {
            "name": name,
            "passed": not problems,
            "problems": problems,
            "seconds": round(time.perf_counter() - started, 2),
            "turn_seconds_max": max((t["seconds"] for t in turns), default=0),
            "usage": usage,
            "order": order_info,
            "turns": turns,
        }
        print(f"  → {'PASS' if not problems else 'FAIL: ' + '; '.join(problems)}"
              f"   [{result['seconds']}s, {usage['prompt_tokens']}+{usage['completion_tokens']} tok, "
              f"cached {usage['cached_tokens']}, ${usage['cost_usd']}]\n")
        results.append(result)

    total = meter.since(0)
    report = {"model": model, "media_model": media_model, "date": date.today().isoformat(),
              "passed": sum(r["passed"] for r in results), "scenarios": len(results),
              "usage": total, "results": results}
    out_dir = REPO / "benchmarks"
    out_dir.mkdir(exist_ok=True)
    stem = f"{label}_{date.today().isoformat()}"
    (out_dir / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Benchmark: {model} (media: {media_model}), {report['date']}", "",
             f"**{report['passed']}/{report['scenarios']} passed** -- {total['calls']} calls, "
             f"{total['prompt_tokens']} prompt / {total['completion_tokens']} completion tokens, "
             f"{total['cached_tokens']} cached, ${total['cost_usd']}", "",
             "| scenario | result | seconds | max turn s | prompt tok | cached | cost $ | problems |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        u = r["usage"]
        lines.append(f"| {r['name']} | {'PASS' if r['passed'] else 'FAIL'} | {r['seconds']} | "
                     f"{r['turn_seconds_max']} | {u['prompt_tokens']} | {u['cached_tokens']} | "
                     f"{u['cost_usd']} | {'; '.join(r['problems'])[:160]} |")
    (out_dir / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if report["passed"] == report["scenarios"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run just the named scenario")
    ap.add_argument("--label", help="file-name stem (default: the model id)")
    args = ap.parse_args()
    sys.exit(run(args.only, args.label))
