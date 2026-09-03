"""Real-pipeline eval harness for the order flow.

Not part of the pytest suite. Drives the real agent loop
(`assistant.agent.run_turn`) against the real OpenRouter model, but against a
throwaway SQLite database and the in-memory fake Shopify shelf
(`tests/fake_shopify.py`) -- never live Shopify, never a real order.

Usage:

    python scripts/eval_order_flow.py [--happy N] [--confused N] [--out DIR]

Writes a JSONL transcript log and a Markdown report to --out (default:
scripts/_eval_output/, not committed -- see .gitignore).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 1. Force a throwaway SQLite database BEFORE anything imports config/settings
#    or domain.db, mirroring tests/conftest.py's pattern exactly.
# ---------------------------------------------------------------------------
#    The path is overridable so a second eval script (scripts/eval_abandonment.py)
#    can run alongside this one without the two fighting over one SQLite file.
SCRATCH_DB = Path(
    os.environ.get("EVAL_SCRATCH_DB")
    or (PROJECT_ROOT / "scripts" / "_eval_output" / "eval_wanas.db")
)
SCRATCH_DB.parent.mkdir(parents=True, exist_ok=True)
if SCRATCH_DB.exists():
    SCRATCH_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{SCRATCH_DB}"
os.environ["LLM_PROVIDER"] = "openrouter"
os.environ["MESSAGE_DEBOUNCE_SECONDS"] = "0"
os.environ["HARNESS_ENABLED"] = "0"
# Hard requirement: this process must never be able to reach live Shopify,
# whatever happens to be sitting in .env. Blanked here, before config.settings
# loads .env, exactly like tests/conftest.py blanks them for the pytest suite.
os.environ["SHOPIFY_STORE_DOMAIN"] = ""
os.environ["SHOPIFY_ADMIN_TOKEN"] = ""
# Don't spam a real inbox/staff queue mailer while running dozens of
# conversations that deliberately include handoffs and cancellations.
os.environ["ALERT_EMAIL_TO"] = ""
os.environ["GMAIL_REFRESH_TOKEN"] = ""
os.environ["DASHBOARD_SESSION_SECRET"] = ""

# config/settings.py calls load_dotenv(PROJECT_ROOT / ".env") at import time,
# which will NOT override the blanks above (dotenv defaults to not
# overwriting already-set env vars), but WILL fill in OPENROUTER_API_KEY /
# LLM_MODEL from .env since those are not set yet. That is exactly what we
# want: the real key, a fake Shopify.

from sqlalchemy.orm import Session  # noqa: E402

from assistant import agent, session as assistant_session  # noqa: E402
from assistant.providers.openrouter import OpenRouterProvider  # noqa: E402
from config.settings import settings  # noqa: E402
from domain.db import SessionLocal, engine  # noqa: E402
from domain.models import Base, ShippingRate  # noqa: E402
from domain.seed.governorates import import_governorates  # noqa: E402
from domain.seed.products import import_products  # noqa: E402
from domain.services import conversation_reset  # noqa: E402

conversation_reset.register_history_clearer(assistant_session.clear)

CHANNEL = "whatsapp"


def _rebuild_schema() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def _install_fake_shopify(db: Session):
    """Same wiring tests/conftest.py's `shopify` fixture does, without
    pytest's monkeypatch fixture: a plain object with the same
    setattr/restore contract."""
    from tests.fake_shopify import FakeShopify

    class _FakeMonkeypatch:
        def __init__(self):
            self._orig: list[tuple[object, str, object]] = []

        def setattr(self, obj, name, value):
            self._orig.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._orig):
                setattr(obj, name, value)
            self._orig.clear()

    mp = _FakeMonkeypatch()
    fake = FakeShopify()
    fake.install(mp)
    fake.seed_from(db)
    db.commit()
    return fake, mp


def _set_shipping_fees(db: Session, fees: dict[str, int]) -> None:
    for key, fee in fees.items():
        rate = db.get(ShippingRate, key)
        if rate is not None:
            rate.fee = fee
    db.commit()


# ---------------------------------------------------------------------------
# Tool-call capture: wrap assistant.agent.call_tool for the duration of one
# conversation, recording (name, arguments, result) for every call.
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    result: object
    is_error: bool = False


@dataclass
class Turn:
    customer_text: str
    bot_text: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    error: str | None = None
    attachments: list[str] = field(default_factory=list)
    silent: bool = False
    elapsed_s: float = 0.0


@dataclass
class ConversationLog:
    scenario_id: str
    label: str
    external_id: str
    turns: list[Turn] = field(default_factory=list)
    exception: str | None = None


class _ToolCallCapture:
    """Context manager that wraps assistant.agent.call_tool for its lifetime,
    appending every (name, arguments, result) triple onto `sink`."""

    def __init__(self, sink: list[ToolCallRecord]):
        self.sink = sink
        self._orig = None

    def __enter__(self):
        self._orig = agent.call_tool

        def wrapped(ctx, name, arguments):
            args = arguments if isinstance(arguments, dict) else {}
            result = self._orig(ctx, name, arguments)
            is_error = isinstance(result, dict) and "error" in result
            self.sink.append(ToolCallRecord(name=name, arguments=args, result=result, is_error=is_error))
            return result

        agent.call_tool = wrapped
        return self

    def __exit__(self, *exc):
        agent.call_tool = self._orig
        return False


def run_conversation(
    db: Session, provider, scenario_id: str, label: str, messages: list[str]
) -> ConversationLog:
    external_id = f"20100{uuid.uuid4().hex[:10]}"
    log = ConversationLog(scenario_id=scenario_id, label=label, external_id=external_id)
    for text in messages:
        turn_calls: list[ToolCallRecord] = []
        t0 = time.monotonic()
        try:
            with _ToolCallCapture(turn_calls):
                reply = agent.run_turn(db, CHANNEL, external_id, text, provider=provider)
            db.commit()
        except Exception:
            db.rollback()
            log.exception = traceback.format_exc()
            log.turns.append(
                Turn(customer_text=text, bot_text="", tool_calls=turn_calls, error="harness_exception")
            )
            break
        elapsed = time.monotonic() - t0
        log.turns.append(
            Turn(
                customer_text=text,
                bot_text=reply.text,
                tool_calls=turn_calls,
                error=reply.error,
                attachments=list(reply.attachments or []),
                silent=reply.silent,
                elapsed_s=elapsed,
            )
        )
    return log


# ---------------------------------------------------------------------------
# Scenario scripts
# ---------------------------------------------------------------------------
# Real catalog (data/products_seed.json), spread across products/colors/sizes.
# Real governorates (data/governorates.json). "Nasr" as an unseeded/no-rate
# governorate check uses a real seeded one with no fee set (Aswan, left with
# fee=None by default -- see _set_shipping_fees below, which only prices a
# subset).

PRICED_GOVERNORATES = [
    "Cairo", "Giza", "Alexandria", "Qalyubia", "Sharqia", "Dakahlia",
    "Gharbia", "Monufia", "Beheira", "Port Said", "Ismailia", "Suez",
    "Beni Suef", "Faiyum", "Minya", "Asyut", "Sohag", "Qena", "Luxor", "Aswan",
]
UNPRICED_GOVERNORATE = "Matrouh"  # deliberately left with no rate


def build_happy_path_scenarios(n: int) -> list[tuple[str, list[str]]]:
    """~n distinct happy-path conversations: browse -> pick -> address ->
    confirm, plus a handful of after-order follow-ups."""

    products = [
        ("Boxy WNS Tee", ["Black", "Grey", "Olive"], ["S", "M", "L", "XL"]),
        ("Cairokee T-shirt", ["Brown", "Black"], ["S", "M", "L", "XL"]),
        ("Cairokee T-shirt 2", ["Black", "White"], ["S", "M", "L", "XL"]),
        ("Envy T-shirt", ["Grey"], ["S", "M", "L", "XL"]),
        ("Ringer Tee", ["Beige", "Brown", "Burgundy", "Navy"], ["S", "M", "L", "XL"]),
        ("Cairokee Hoodie", ["Brown", "Black"], ["S", "M", "L", "XL"]),
        ("WANAS Crewneck", ["Burgundy", "Navy", "Olive"], ["S", "M", "L", "XL"]),
        ("WANAS Hoodie", ["Black", "Grey", "Olive"], ["S", "M", "L", "XL"]),
        ("WANAS Quarter-Zip", ["Camel Brown", "Light Brown", "Navy"], ["S", "M", "L", "XL"]),
        ("WANAS Zip-Hoodie", ["Black", "Grey", "Olive"], ["S", "M", "L", "XL"]),
        ("Zipup", ["Black", "Pink", "Vintage Green"], ["S", "M", "L", "XL"]),
        ("Knitted Polo", ["Burgundy", "Navy", "Olive", "White"], ["S", "M", "L", "XL"]),
        ("WANAS Polo", ["Black", "Grey", "Olive"], ["S", "M", "L", "XL"]),
        ("Lightweight Sweatpant", ["Black", "Grey", "Navy"], ["S", "M", "L", "XL"]),
        ("WANAS Sweatpant", ["Black", "Grey", "Olive"], ["S", "M", "L", "XL"]),
        ("Worker Jacket", ["Black", "Olive"], ["S", "M", "L", "XL"]),
        ("Feelin Fine Top", ["Black", "Olive", "Pink", "White"], ["S", "M", "L"]),
        ("Heart Top", ["Black", "Olive", "Pink", "White"], ["S", "M", "L"]),
    ]

    names = [
        "أحمد محمد", "ساره علي", "Mohamed Tarek", "نور الهدى", "كريم عبد الله",
        "ياسمين حسن", "عمر خالد", "مريم سيد", "Ali Hassan", "هبة فاروق",
        "طارق سليم", "دينا أشرف", "Youssef Nabil", "منة الله سامي", "حسام الدين",
    ]

    phone_formats = [
        lambda: "01012345678",
        lambda: "+201098765432",
        lambda: "0106 543 2109",
        lambda: "+20 111 234 5678",
        lambda: "01234567890",
        lambda: "0020122345566",
    ]

    addresses = [
        "شارع التحرير، عمارة 5، الدور التالت، شقة 12",
        "12 شارع الهرم، بجوار صيدلية العزبي",
        "مدينة نصر، شارع مكرم عبيد، عمارة 20",
        "المعادي، شارع 9، فيلا 3",
        "الشيخ زايد، الحي الأول، فيلا 45",
        "أسيوط الجديدة، عمارة 8، شقة 4",
    ]

    scenarios: list[tuple[str, list[str]]] = []

    # A. Everything in one message, varied phrasing/products/governorates.
    templates_all_at_once = [
        "عايز {product} {color} مقاس {size} وعدد {qty}",
        "ابعتلي {product} لون {color} {size} كمية {qty}",
        "{product} {color} {size} x{qty} من فضلك",
        "محتاج ال {product} ب{color} مقاس {size}، عايز {qty}",
        "3ayez {product} {color} {size}, {qty} 2ta3",
    ]

    idx = 0
    n_all_at_once = max(1, n // 3)
    for i in range(n_all_at_once):
        product, colors, sizes = products[idx % len(products)]
        color = colors[i % len(colors)]
        size = sizes[i % len(sizes)]
        qty = 1 + (i % 3)
        template = templates_all_at_once[i % len(templates_all_at_once)]
        opening = template.format(product=product, color=color, size=size, qty=qty)
        name = names[i % len(names)]
        gov = PRICED_GOVERNORATES[i % len(PRICED_GOVERNORATES)]
        phone = phone_formats[i % len(phone_formats)]()
        address = addresses[i % len(addresses)]
        scenarios.append((
            f"happy_all_at_once_{i}",
            [
                opening,
                "أيوه أكد الطلب",
                f"اسمي {name}، محافظة {gov}، العنوان: {address}، رقم التليفون {phone}",
            ],
        ))
        idx += 1

    # B. Step by step: product -> size -> color -> qty -> confirm -> details.
    n_step_by_step = max(1, n // 3)
    for i in range(n_step_by_step):
        product, colors, sizes = products[(idx) % len(products)]
        color = colors[(i + 1) % len(colors)]
        size = sizes[(i + 2) % len(sizes)]
        name = names[(i + 5) % len(names)]
        gov = PRICED_GOVERNORATES[(i + 7) % len(PRICED_GOVERNORATES)]
        phone = phone_formats[(i + 2) % len(phone_formats)]()
        address = addresses[(i + 3) % len(addresses)]
        msgs = [
            f"عندكم {product}؟",
            f"عايز لون {color}",
            f"مقاس {size} متاح؟",
            "تمام، حطهولي في الطلب",
            f"اسمي {name} وعايز أأكد الأوردر",
            f"محافظة {gov}، {address}، تليفون {phone}",
        ]
        scenarios.append((f"happy_step_by_step_{i}", msgs))
        idx += 1

    # C. Franco-Arabic + English mixed, terse, varied detail.
    n_franco = max(1, n - len(scenarios) - 8)  # leave room for after-order batch
    franco_templates = [
        "3ayz {product} {color} {size}",
        "momken a3raf as3ar el {product}?",
        "{product} feh {color}?",
    ]
    for i in range(n_franco):
        product, colors, sizes = products[(idx) % len(products)]
        color = colors[(i + 2) % len(colors)]
        size = sizes[(i + 1) % len(sizes)]
        name = names[(i + 9) % len(names)]
        gov = PRICED_GOVERNORATES[(i + 3) % len(PRICED_GOVERNORATES)]
        phone = phone_formats[(i + 4) % len(phone_formats)]()
        address = addresses[(i + 1) % len(addresses)]
        opening = franco_templates[i % len(franco_templates)].format(product=product, color=color, size=size)
        follow = (
            f"{product} {color} {size}, 3ayez wa7ed"
            if "as3ar" in opening or "feh" in opening
            else "tmam, 7oto fel order"
        )
        msgs = [
            opening,
            follow,
            f"esmy {name}, {gov}, {address}, {phone}",
            "aywa akked el order",
        ]
        scenarios.append((f"happy_franco_{i}", msgs))
        idx += 1

    # D. Include one deliberately unpriced governorate (Matrouh, no_rate_set).
    product, colors, sizes = products[idx % len(products)]
    scenarios.append((
        "happy_unpriced_governorate",
        [
            f"عايز {product} {colors[0]} مقاس {sizes[0]}",
            "أيوه اتفضل",
            f"اسمي محمود سعيد، محافظة {UNPRICED_GOVERNORATE}، شارع الميناء عمارة 3، تليفون 01055566677",
        ],
    ))

    # E. After-order-placed follow-ups: status / modify qty / cancel.
    for i in range(4):
        product, colors, sizes = products[(idx + i) % len(products)]
        color = colors[i % len(colors)]
        size = sizes[i % len(sizes)]
        name = names[(i + 11) % len(names)]
        gov = PRICED_GOVERNORATES[(i + 12) % len(PRICED_GOVERNORATES)]
        phone = phone_formats[(i + 1) % len(phone_formats)]()
        address = addresses[(i + 5) % len(addresses)]
        follow_up = ["عايز أعرف حالة أوردري", "ممكن أزود الكمية لـ 3؟", "عايز ألغي الأوردر"][i % 3]
        msgs = [
            f"عايز {product} {color} مقاس {size}",
            "أيوه أكد",
            f"اسمي {name}، {gov}، {address}، {phone}",
            follow_up,
        ]
        scenarios.append((f"happy_followup_{i}", msgs))

    return scenarios[:n] if n < len(scenarios) else scenarios


def build_confused_scenarios(n: int) -> list[tuple[str, list[str]]]:
    products = [
        ("WANAS Hoodie", "Black", "L"),
        ("Cairokee T-shirt", "Brown", "M"),
        ("Zipup", "Pink", "S"),
        ("Worker Jacket", "Olive", "XL"),
        ("Knitted Polo", "Navy", "M"),
        ("WANAS Sweatpant", "Grey", "L"),
        ("Ringer Tee", "Burgundy", "S"),
        ("WANAS Quarter-Zip", "Navy", "XL"),
    ]

    scenarios: list[tuple[str, list[str]]] = []

    # 1. Vague replies to a direct question.
    scenarios.append((
        "confused_vague_size",
        [
            "عايز هودي",
            "الأسود",
            "مش عارف مقاسي، اي حاجة كده",
            "طيب لارج يبقى",
            "اسمي محمد، القاهرة، شارع النصر عمارة 4، 01011122233",
            "أيوه أكد",
        ],
    ))

    # 2. Off-topic mid-address-collection.
    scenarios.append((
        "confused_offtopic_mid_address",
        [
            "عايز Cairokee T-shirt أسود L",
            "أيوه حطه",
            "بس هو الشحن بيتأخر قد ايه؟",
            "طب تمام، اسمي سارة، الجيزة، شارع البطل أحمد عبد العزيز عمارة 9، 01234455667",
        ],
    ))

    # 3. Changes mind about color after already stating one.
    scenarios.append((
        "confused_change_mind_color",
        [
            "عايز Zipup لون فيرت",
            "لا بس فكرت، عايزه أسود",
            "أيوه مقاس M",
            "خلاص أكد",
            "اسمي كريم، الإسكندرية، شارع النصر، 01099887766",
        ],
    ))

    # 4. Repeats something already said.
    scenarios.append((
        "confused_repeats_self",
        [
            "عايز WANAS Hoodie أسود",
            "عايز WANAS Hoodie أسود مقاس L",
            "عايز WANAS Hoodie أسود",
            "تمام أكد الطلب",
            "اسمي ياسمين، الشرقية، شارع الجمهورية، 01155566677",
        ],
    ))

    # 5. Non-answer: bot asks governorate, customer names a village not on
    #    the list.
    scenarios.append((
        "confused_village_not_on_list",
        [
            "عايز Worker Jacket أسود XL",
            "أيوه اتفضل",
            "اسمي أحمد، أنا من قرية كفر الشيخ الصغيرة، شارع المدرسة، 01022233344",
            "طيب محافظة كفر الشيخ يبقى",
        ],
    ))

    # 6. Answers a totally different question than asked.
    scenarios.append((
        "confused_answers_different_question",
        [
            "عايز Knitted Polo",
            "نيفي",
            "M",
            "تمام أكد",
            "اسمي وائل",
            "بتقفلوا الساعة كام؟",
            "القاهرة، شارع الثورة عمارة 2، 01000011122",
        ],
    ))

    # 7. Goes quiet then returns to something unrelated then back to order.
    scenarios.append((
        "confused_detour_then_return",
        [
            "عايز WANAS Sweatpant رمادي L",
            "أيوه أكد",
            "بس ايه رأيك في الشحن للمحافظات؟",
            "طيب رجعنالطلب، اسمي منة، أسيوط، شارع الجلاء، 01288899900",
        ],
    ))

    # 8. Partial answer: gives only phone, not address, when both asked.
    scenarios.append((
        "confused_partial_answer",
        [
            "عايز Ringer Tee بورجاندي S",
            "أيوه أكد",
            "اسمي هالة",
            "01077788899",
            "القاهرة",
            "شارع رمسيس، عمارة 15، الدور الخامس",
        ],
    ))

    # 9. Contradicts themselves on quantity.
    scenarios.append((
        "confused_contradicts_quantity",
        [
            "عايز WANAS Quarter-Zip نيفي XL، عدد 2",
            "لا بس عايز واحد بس",
            "لا خليها 2 تاني",
            "تمام أكد",
            "اسمي عمر، قنا، شارع الكورنيش، 01166677788",
        ],
    ))

    # 10. Explicit request for a human -- should be honoured, not brushed off.
    scenarios.append((
        "confused_explicit_human_request",
        [
            "عندي مشكلة في أوردر قديم مش لاقيه، عايز أكلم حد حقيقي من عندكم",
        ],
    ))

    # 11. A genuinely vague opening that must NOT trigger request_human.
    scenarios.append((
        "confused_vague_not_human_worthy",
        [
            "مش عارف عايز ايه بصراحة",
            "اي حاجة كده حلوة",
            "طب وريني تيشرتات",
        ],
    ))

    # 12. Non-answer mid-flow: bot asks size, customer answers with color.
    scenarios.append((
        "confused_wrong_field_answer",
        [
            "عايز Feelin Fine Top",
            "أسود",  # given as color when asked which product variant, fine
            "مقاسي ايه ملهوش لازمة، عايز الأصغر",
            "S يبقى",
            "تمام أكد",
            "اسمي دينا، بورسعيد، شارع 23 يوليو، 01344455566",
        ],
    ))

    # 13. Says yes/confirms too early before all info given.
    scenarios.append((
        "confused_confirms_too_early",
        [
            "عايز Heart Top وردي",
            "أيوه أكد الأوردر",  # confirms before size/address/phone given
            "S",
            "اسمي ندى، دمياط، شارع بورسعيد، 01522233344",
        ],
    ))

    # Fill remaining with generated variety cycling through products.
    templates = [
        [
            "عايز {p}", "{c}", "مش متأكد من المقاس", "خليه {s}", "تمام أكد",
            "اسمي سيف، المنوفية، شارع الجيش، 01611122233",
        ],
        [
            "{p} متاح؟", "طب {c}", "{s}", "استنى شوية", "تمام أكد الأوردر",
            "اسمي رنا، الغربية، شارع سعد زغلول، 01711223344",
        ],
    ]
    i = 0
    while len(scenarios) < n:
        p, c, s = products[i % len(products)]
        template = templates[i % len(templates)]
        msgs = [m.format(p=p, c=c, s=s) for m in template]
        scenarios.append((f"confused_extra_{i}", msgs))
        i += 1

    return scenarios[:n]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_conversation(log: ConversationLog) -> dict:
    """Structured verdict for one conversation. Cross-checks tool results and
    text, but does not attempt semantic judgement beyond what is mechanical
    (number cross-checking, error-after-recovery checks)."""

    verdict = {
        "scenario_id": log.scenario_id,
        "label": log.label,
        "order_placed": False,
        "order_id": None,
        "request_human_called": False,
        "request_human_justified": None,
        "tool_errors": [],
        "hallucination_flags": [],
        "ignored_error_and_claimed_success": False,
        "failure_categories": [],
    }

    if log.exception:
        verdict["failure_categories"].append("harness_exception")
        return verdict

    all_tool_calls: list[tuple[int, ToolCallRecord]] = []
    for ti, turn in enumerate(log.turns):
        for tc in turn.tool_calls:
            all_tool_calls.append((ti, tc))

    for ti, tc in all_tool_calls:
        if tc.name == "confirm_order" and isinstance(tc.result, dict) and tc.result.get("order_id"):
            verdict["order_placed"] = True
            verdict["order_id"] = tc.result["order_id"]
        if tc.name == "request_human":
            verdict["request_human_called"] = True
        if tc.is_error:
            verdict["tool_errors"].append(
                {"turn": ti, "tool": tc.name, "arguments": tc.arguments, "result": tc.result}
            )

    if verdict["request_human_called"]:
        justified_words = ["إنسان", "حقيقي", "موظف", "human", "مشكلة", "شكوى"]
        first_customer_texts = " ".join(t.customer_text for t in log.turns)
        verdict["request_human_justified"] = any(w in first_customer_texts for w in justified_words)
        if not verdict["request_human_justified"]:
            verdict["failure_categories"].append("premature_or_incorrect_human_handoff")

    if verdict["tool_errors"]:
        verdict["failure_categories"].append("tool_call_error")
        # Check what happened after an error: did the bot's next reply in the
        # same or later turn claim success anyway?
        for err in verdict["tool_errors"]:
            ti = err["turn"]
            bot_text = log.turns[ti].bot_text if ti < len(log.turns) else ""
            success_words = ["تم تأكيد", "تم الطلب", "الأوردر اتأكد", "confirmed", "order_id"]
            if any(w in bot_text for w in success_words) and err["tool"] == "confirm_order":
                verdict["ignored_error_and_claimed_success"] = True
                verdict["failure_categories"].append("hallucinated_confirmation")

    # Hallucination check: does any bot reply state a specific price/order
    # number/stock figure that does not appear in any preceding tool result
    # this turn or earlier in the conversation?
    seen_numbers: set[str] = set()
    for turn in log.turns:
        for tc in turn.tool_calls:
            seen_numbers.update(_extract_numbers(tc.result))
        bot_numbers = _extract_numbers(turn.bot_text)
        # Only flag numbers that look like a price/order number (3+ digits),
        # not small quantities/sizes that are conversational.
        suspicious = {n for n in bot_numbers if len(n) >= 3 and n not in seen_numbers}
        if suspicious:
            verdict["hallucination_flags"].append(
                {"turn": turn.customer_text, "bot_text": turn.bot_text, "numbers": sorted(suspicious)}
            )

    if verdict["hallucination_flags"] and "hallucinated_confirmation" not in verdict["failure_categories"]:
        verdict["failure_categories"].append("suspected_hallucinated_fact")

    return verdict


def _extract_numbers(value) -> set[str]:
    import re

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return set(re.findall(r"\d{3,}", text or ""))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _turn_to_dict(turn: Turn) -> dict:
    return {
        "customer": turn.customer_text,
        "bot": turn.bot_text,
        "error": turn.error,
        "silent": turn.silent,
        "attachments": turn.attachments,
        "elapsed_s": round(turn.elapsed_s, 2),
        "tool_calls": [
            {"name": tc.name, "arguments": tc.arguments, "result": tc.result, "is_error": tc.is_error}
            for tc in turn.tool_calls
        ],
    }


def write_jsonl(path: Path, logs: list[ConversationLog], verdicts: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for log, verdict in zip(logs, verdicts, strict=True):
            record = {
                "scenario_id": log.scenario_id,
                "label": log.label,
                "external_id": log.external_id,
                "exception": log.exception,
                "turns": [_turn_to_dict(t) for t in log.turns],
                "verdict": verdict,
            }
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _transcript_md(log: ConversationLog) -> str:
    lines = [f"**Scenario `{log.scenario_id}` ({log.label})** — external_id `{log.external_id}`\n"]
    for turn in log.turns:
        lines.append(f"- customer: {turn.customer_text}")
        for tc in turn.tool_calls:
            marker = "ERROR " if tc.is_error else ""
            call = json.dumps(tc.arguments, ensure_ascii=False, default=str)
            result = json.dumps(tc.result, ensure_ascii=False, default=str)[:500]
            lines.append(f"  - {marker}tool `{tc.name}({call})` -> `{result}`")
        bot_line = turn.bot_text or (
            "(silent — already answered by a tool)" if turn.silent else "(empty reply)"
        )
        lines.append(f"  - bot: {bot_line}")
        if turn.error:
            lines.append(f"  - turn error: {turn.error}")
    if log.exception:
        lines.append(f"- HARNESS EXCEPTION:\n```\n{log.exception}\n```")
    return "\n".join(lines)


def write_report(
    path: Path,
    happy_logs: list[ConversationLog],
    happy_verdicts: list[dict],
    confused_logs: list[ConversationLog],
    confused_verdicts: list[dict],
) -> None:
    def success_rate(verdicts):
        if not verdicts:
            return 0.0
        return sum(1 for v in verdicts if v["order_placed"]) / len(verdicts)

    def failure_breakdown(verdicts):
        counts: dict[str, int] = {}
        for v in verdicts:
            for cat in v["failure_categories"]:
                counts[cat] = counts.get(cat, 0) + 1
        return counts

    happy_breakdown = failure_breakdown(happy_verdicts)
    confused_breakdown = failure_breakdown(confused_verdicts)
    combined_breakdown: dict[str, int] = {}
    for d in (happy_breakdown, confused_breakdown):
        for k, v in d.items():
            combined_breakdown[k] = combined_breakdown.get(k, 0) + v

    all_logs = happy_logs + confused_logs
    all_verdicts = happy_verdicts + confused_verdicts
    by_id = {log.scenario_id: log for log in all_logs}

    lines = ["# Order-flow eval report\n"]
    lines.append(f"Happy path: {len(happy_logs)} conversations, "
                 f"order-completion success rate = {success_rate(happy_verdicts):.1%}\n")
    lines.append(f"Confused-customer batch: {len(confused_logs)} conversations, "
                 f"order-completion success rate = {success_rate(confused_verdicts):.1%}\n")

    lines.append("\n## Failure category breakdown\n")
    if not combined_breakdown:
        lines.append("No failure categories were triggered.\n")
    for cat, count in sorted(combined_breakdown.items(), key=lambda kv: -kv[1]):
        lines.append(f"- **{cat}**: {count}")

    lines.append("\n## Examples per failure category\n")
    for cat in sorted(combined_breakdown):
        lines.append(f"\n### {cat}\n")
        examples = [v for v in all_verdicts if cat in v["failure_categories"]][:3]
        for v in examples:
            log = by_id[v["scenario_id"]]
            lines.append(_transcript_md(log))
            lines.append("")

    lines.append("\n## All scenario verdicts\n")
    lines.append(
        "| scenario | batch | order placed | order id | request_human | tool errors | failure categories |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for v, batch in [(v, "happy") for v in happy_verdicts] + [(v, "confused") for v in confused_verdicts]:
        cats = ", ".join(v["failure_categories"]) or "-"
        lines.append(
            f"| {v['scenario_id']} | {batch} | {v['order_placed']} | {v['order_id'] or ''} | "
            f"{v['request_human_called']} | {len(v['tool_errors'])} | {cats} |"
        )

    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--happy", type=int, default=100)
    parser.add_argument("--confused", type=int, default=28)
    parser.add_argument("--out", type=str, default=str(PROJECT_ROOT / "scripts" / "_eval_output"))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    _rebuild_schema()
    db = SessionLocal()
    import_products(db)
    import_governorates(db)
    db.commit()
    fake_shopify, mp = _install_fake_shopify(db)

    fees = {gov: 60 + (i % 5) * 10 for i, gov in enumerate(PRICED_GOVERNORATES)}
    _set_shipping_fees(db, fees)
    # UNPRICED_GOVERNORATE deliberately left with fee=None.

    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set (checked .env and environment). Aborting.", file=sys.stderr)
        sys.exit(1)
    if settings.shopify_store_domain or settings.shopify_admin_token:
        print("Refusing to run: live Shopify credentials are present in this process's "
              "settings. This script must never touch the real store.", file=sys.stderr)
        sys.exit(1)

    provider = OpenRouterProvider(api_key=settings.openrouter_api_key, model=settings.llm_model or "")

    happy_scenarios = build_happy_path_scenarios(args.happy)
    confused_scenarios = build_confused_scenarios(args.confused)

    print(f"Running {len(happy_scenarios)} happy-path conversations...")
    happy_logs: list[ConversationLog] = []
    for i, (sid, msgs) in enumerate(happy_scenarios):
        print(f"  [{i + 1}/{len(happy_scenarios)}] {sid}")
        happy_logs.append(run_conversation(db, provider, sid, "happy", msgs))

    print(f"Running {len(confused_scenarios)} confused-customer conversations...")
    confused_logs: list[ConversationLog] = []
    for i, (sid, msgs) in enumerate(confused_scenarios):
        print(f"  [{i + 1}/{len(confused_scenarios)}] {sid}")
        confused_logs.append(run_conversation(db, provider, sid, "confused", msgs))

    mp.undo()
    db.close()

    happy_verdicts = [evaluate_conversation(log) for log in happy_logs]
    confused_verdicts = [evaluate_conversation(log) for log in confused_logs]

    jsonl_path = out_dir / "transcripts.jsonl"
    write_jsonl(jsonl_path, happy_logs + confused_logs, happy_verdicts + confused_verdicts)

    report_path = PROJECT_ROOT / "docs" / "eval_order_flow_report.md"
    write_report(report_path, happy_logs, happy_verdicts, confused_logs, confused_verdicts)

    print(f"\nTranscripts: {jsonl_path}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
