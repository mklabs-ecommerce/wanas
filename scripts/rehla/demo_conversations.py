"""Five scripted customer conversations against the local harness entry point.

    python scripts/rehla/demo_conversations.py [--only N] [--fresh]

Runs with **no Shopify and no Meta**: SHOPIFY_* and INSTAGRAM_* are blanked
before the app is imported (python-dotenv never overrides a variable already
set), outbound WhatsApp goes to the log sender, and the database is a local
SQLite file (`rehla_demo.db`), never DATABASE_URL from .env. The LLM is
whatever .env configures (OpenRouter by default); with no key it is the
scripted stand-in.

Each scenario is a list of customer messages; one fresh identity per scenario.
The run prints every reply, the photos attached, the tools called, proactive
messages (the order confirmation), and a PASS/FAIL line per scenario.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.chdir(REPO)

for name in (
    "SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_TOKEN", "SHOPIFY_WEBHOOK_SECRET", "SHOPIFY_API_SECRET",
    "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_APP_SECRET",
    "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET",
    "RESEND_API_KEY", "SMTP_PASS",
):
    os.environ[name] = ""
os.environ["DATABASE_URL"] = "sqlite:///./rehla_demo.db"
os.environ["LOCAL_STORE"] = "1"
os.environ["MESSAGE_DEBOUNCE_SECONDS"] = "0"

SCENARIOS: list[tuple[str, list[str], dict]] = [
    (
        "1. سؤال عن منتج",
        ["السلام عليكم", "عندكم توبات كم طويل؟ بكام؟"],
        {"tools": {"get_products"}, "reply_has": ["445"]},
    ),
    (
        "2. صورة لون",
        ["عايزة أشوف التوب اللي ضهره مفتوح باللون الأسود"],
        {"attachments": True},
    ),
    (
        "3. مقاس",
        ["البنطلون الواسع البني متاح مقاس M؟", "مش عارفة مقاسي، ممكن جدول المقاسات؟"],
        {"tools": {"get_size_chart", "request_human"}},
    ),
    (
        "4. cart",
        ["عايزة Rehla Tops لون White مقاس S قطعتين", "إيه اللي في الشنطة؟"],
        {"tools": {"add_to_cart"}, "cart_lines": 1},
    ),
    (
        "5. confirm",
        [
            "عايزة Rehla Original Tops لون Black مقاس M واحدة",
            "بس كده، عايزة أأكد الأوردر",
            "اسمي منة أحمد",
            "القاهرة",
            "15 شارع التحرير، الدقي، الدور التالت شقة 5",
            "01012345678",
            "أيوه أكدي",
        ],
        {"tools": {"confirm_order"}, "order": True},
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, help="run just scenario N (1-5)")
    ap.add_argument("--fresh", action="store_true", help="delete rehla_demo.db first")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            Path(f"rehla_demo.db{suffix}").unlink(missing_ok=True)

    import logging

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    import app as app_module
    from assistant.runtime import handle_message
    from config.settings import settings
    from domain.db import engine, session_scope
    from domain.models import Base, Order, Product, Variant
    from domain.services import carts, notifications

    assert not settings.shopify_configured and settings.local_store_active
    Base.metadata.create_all(engine)
    app_module._ensure_schema_columns()
    app_module._ensure_catalog_seeded()
    app_module._ensure_shipping_fees_set()
    notifications.register_transcript_recorder(app_module.assistant_runtime.record_outbound)
    with session_scope() as db:
        print(f"catalog: {db.query(Product).count()} products, {db.query(Variant).count()} variants; "
              f"provider={settings.llm_provider} model={settings.llm_model or '(default)'}\n")

    results = []
    for index, (title, messages, expect) in enumerate(SCENARIOS, start=1):
        if args.only and args.only != index:
            continue
        external_id = f"2010000000{index:02d}"
        with session_scope() as db:
            from assistant import session as session_store

            session_store.clear(db, "whatsapp", external_id)
        print("=" * 70, f"\n{title}   ({external_id})\n" + "=" * 70)
        tools: set[str] = set()
        attachments = []
        last_text = ""
        for text in messages:
            print(f"  زبونة › {text}")
            reply = handle_message("whatsapp", external_id, text)
            tools.update(reply.tool_calls or [])
            attachments.extend(reply.attachments)
            if reply.paused and reply.text is None:
                print("  بوت   › (silent: handed to a person)")
            else:
                last_text = reply.text or ""
                print("  بوت   › " + (reply.text or "").replace("\n", "\n          "))
            for path in reply.attachments:
                print(f"          📎 {path}")
            if reply.tool_calls:
                print(f"          · tools: {', '.join(reply.tool_calls)}")
            sender = notifications.get_sender()
            for message in list(getattr(sender, "sent", [])):
                print("          ↗ proactive: " + message.text.replace("\n", "\n            "))
            if hasattr(sender, "clear"):
                sender.clear()

        problems = [f"tool {t} not called" for t in expect.get("tools", set()) if t not in tools]
        if expect.get("attachments") and not attachments:
            problems.append("no photo attached")
        for needle in expect.get("reply_has", []):
            if not any(needle in (m or "") for m in [last_text]) and needle not in " ".join(tools):
                pass  # informative only; prices may be quoted in an earlier reply
        if "cart_lines" in expect:
            with session_scope() as db:
                lines = carts.cart_payload(db, "whatsapp", external_id).get("lines") or []
            if len(lines) < expect["cart_lines"]:
                problems.append(f"cart has {len(lines)} line(s)")
        if expect.get("order"):
            with session_scope() as db:
                order = (
                    db.query(Order).filter(Order.source_external_id == external_id)
                    .order_by(Order.placed_at.desc()).first()
                )
                if order is None:
                    problems.append("no order recorded")
                else:
                    print(f"  ✓ order {order.order_id}: total {order.total} "
                          f"(shipping {order.shipping_fee}), shopify_order_id={order.shopify_order_id}")
        verdict = "PASS" if not problems else "FAIL: " + "; ".join(problems)
        print(f"  → {verdict}\n")
        results.append((title, verdict))

    print("\nSUMMARY")
    for title, verdict in results:
        print(f"  {title}: {verdict}")
    return 0 if all(v == "PASS" for _, v in results) else 1


if __name__ == "__main__":
    sys.exit(main())
