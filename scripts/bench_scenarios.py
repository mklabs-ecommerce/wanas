"""The fixed set of conversations every latency and quality run is measured on.

One list, two consumers: `bench_turn.py` times these and `quality_gate.py`
checks that what comes back still means the same thing. They have to be the
same scenarios or a speed-up and the judgement about whether it cost anything
are not about the same conversations.

They are the six shapes that actually arrive: a greeting, a product question, a
sizing question, adding to the cart, placing the order, and the shipping
question -- which is the one with a single correct stored answer and therefore
the one worth watching for a needless trip through the tool loop.

Each step carries the customer's real words (both modes send exactly these) and
a `plan`: what the fake provider should do for that step, as the hops a
competent model would make. The plan is what makes `LLM_PROVIDER=fake` measure
**our** code rather than a stand-in's idea of a conversation -- the real tool
loop, the real catalog reads, the real session writes, the real number of
round trips, with only the model's own latency removed.

Ids are never hardcoded. `resolve` reads a real product and a real in-stock
variant out of whatever database the run is pointed at, so the same scenarios
work against a seeded local SQLite file and against a copy of production.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from domain.models import Product, Variant


@dataclass
class Step:
    """One customer message, and what the fake provider does with it."""

    #: What the customer types. Sent verbatim in both modes.
    text: str
    #: Hops for the fake provider: each entry is either a list of tool calls
    #: (`[(name, arguments), ...]`) or a string, which ends the turn as the
    #: reply. The last entry is always a string.
    plan: list = field(default_factory=list)
    #: Tool names a correct answer must have called, in any order. The quality
    #: gate reads this; nothing in the timing path does.
    expects_tools: tuple[str, ...] = ()
    #: Substrings the reply must contain (a price, a size) -- filled in from
    #: the resolved catalog, so the gate checks facts and not phrasing.
    expects_text: tuple[str, ...] = ()


@dataclass
class Scenario:
    name: str
    steps: list[Step]


def resolve(session) -> dict:
    """A real product, colour, size and variant to build the scenarios on.

    Picked deterministically -- first by id, in stock, not archived -- so two
    runs of the benchmark are comparable and a failure is reproducible. Raises
    rather than guessing: a benchmark quietly measuring a conversation about a
    product that does not exist is worse than one that will not start.
    """
    rows = session.execute(
        select(Product, Variant)
        .join(Variant, Variant.product_id == Product.product_id)
        .where(Variant.stock_qty > 0)
        .order_by(Product.product_id, Variant.variant_id)
    ).all()
    if not rows:
        raise SystemExit(
            "no in-stock variant in this database -- run `python manage.py seed` first"
        )
    product, variant = rows[0]
    return {
        "product_id": product.product_id,
        "product_name": product.name,
        "variant_id": variant.variant_id,
        "color": variant.color or "",
        "size": variant.size or "",
        "price": f"{int(variant.price)}",
        "category": product.category or "",
    }


def build(facts: dict) -> list[Scenario]:
    """The six scenarios, with the resolved ids folded in."""
    product_id = facts["product_id"]
    variant_id = facts["variant_id"]
    name = facts["product_name"]

    return [
        Scenario(
            "greeting",
            [Step("السلام عليكم", plan=["أهلاً بيك في وناس جاليري. تحب أساعدك في إيه؟"])],
        ),
        Scenario(
            "product_question",
            [
                Step(
                    f"عايز أعرف عن {name}",
                    plan=[
                        [("get_products", {"query": name})],
                        f"{name} متاح عندنا بسعر {facts['price']} جنيه.",
                    ],
                    expects_tools=("get_products",),
                    expects_text=(facts["price"],),
                )
            ],
        ),
        Scenario(
            "sizes",
            [
                Step(
                    f"{name} بيجي مقاسات إيه؟",
                    plan=[
                        [("get_products", {"query": name})],
                        [("get_variants", {"product_id": product_id})],
                        f"متاح مقاس {facts['size']} بسعر {facts['price']} جنيه.",
                    ],
                    expects_tools=("get_variants",),
                    expects_text=(facts["size"],),
                )
            ],
        ),
        Scenario(
            "add_to_cart",
            [
                Step(
                    f"{name} مقاس {facts['size']}",
                    plan=[
                        [("get_products", {"query": name})],
                        [("get_variants", {"product_id": product_id})],
                        f"تمام، {name} مقاس {facts['size']} بـ{facts['price']} جنيه. أحطهولك؟",
                    ],
                    expects_tools=("get_variants",),
                ),
                Step(
                    "أيوه حطهولي",
                    plan=[
                        [("add_to_cart", {"variant_id": variant_id, "quantity": 1})],
                        "اتحط في السلة. تحب تكمل الطلب؟",
                    ],
                    expects_tools=("add_to_cart",),
                ),
            ],
        ),
        Scenario(
            "confirm_order",
            [
                Step(
                    f"{name} مقاس {facts['size']}",
                    plan=[
                        [("get_products", {"query": name})],
                        [("get_variants", {"product_id": product_id})],
                        f"تمام، {name} مقاس {facts['size']} بـ{facts['price']} جنيه.",
                    ],
                ),
                Step(
                    "حطهولي في السلة",
                    plan=[
                        [("add_to_cart", {"variant_id": variant_id, "quantity": 1})],
                        "اتحط في السلة. ابعتلي الاسم والعنوان والمحافظة والتليفون.",
                    ],
                    expects_tools=("add_to_cart",),
                ),
                Step(
                    "أحمد محمد، القاهرة، ٥ شارع التحرير الدقي، ٠١٠٠٠٠٠٠٠٠٠",
                    plan=[
                        [
                            (
                                "confirm_order",
                                {
                                    "customer_name": "أحمد محمد",
                                    "governorate": "Cairo",
                                    "address": "5 شارع التحرير، الدقي",
                                    "contact_phone": "01000000000",
                                },
                            )
                        ],
                        "",
                    ],
                    expects_tools=("confirm_order",),
                ),
            ],
        ),
        Scenario(
            "shipping_question",
            [
                Step(
                    "الشحن كام وبيوصل امتى؟",
                    plan=[
                        [("get_shipping_fee", {"governorate": "Cairo"})],
                        "الشحن للقاهرة ٦٠ جنيه وبيوصل خلال ٢ لـ٤ أيام.",
                    ],
                    expects_tools=("get_shipping_fee",),
                )
            ],
        ),
    ]


#: The one scenario the target is stated against: "an ordinary reply (a product
#: question with one or two tool calls) under 10 seconds".
ORDINARY = "sizes"
