"""The fixed set of conversations every latency and quality run is measured on.

One list, two consumers: `bench_turn.py` times these and `quality_gate.py`
checks that what comes back still means the same thing. They have to be the
same scenarios or a speed-up and the judgement about whether it cost anything
are not about the same conversations.

They are the seven shapes that actually arrive: a greeting, a product question,
a sizing question, adding to the cart, placing the order, the sleeve question,
and the shipping question -- which is the one with a single correct stored answer and therefore
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
    #: This step's reply is about one named garment, so it has to show it. Not
    #: judged against the golden run: a clothes shop answering about a product
    #: in words alone is wrong on its own terms, however consistently it does
    #: it.
    expects_photo: bool = False
    #: The customer asked about sizes, measurements or fit here. The size
    #: chart may only ride along on a step with this set -- everywhere else an
    #: attached chart is a table nobody asked for.
    sizing_question: bool = False


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
    """The seven scenarios, with the resolved ids folded in."""
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
                        f"تيشيرت {name} متاح عندنا بسعر {facts['price']} جنيه. تحب تشوف المقاسات؟",
                    ],
                    expects_tools=("get_products",),
                    expects_text=(facts["price"],),
                    expects_photo=True,
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
                    # Not pinned to `get_variants`: `get_products` already
                    # returns the size list, and a model that answers a sizing
                    # question from it has saved a whole round trip and is not
                    # wrong. What must not happen is answering from nothing,
                    # which the "golden called a tool, this run called none"
                    # rule in `quality_gate.py` is what defends.
                    expects_text=(facts["size"],),
                    expects_photo=True,
                    sizing_question=True,
                )
            ],
        ),
        Scenario(
            "add_to_cart",
            [
                Step(
                    # The colour is named. It has to be: this product comes in
                    # three, and a model that adds one the customer never
                    # chose is the wrong-product sale this codebase refuses
                    # everywhere. Asking "which colour?" is the *correct*
                    # answer to a message without one -- the first real-model
                    # run of this set did exactly that, which made the
                    # scenario a measurement of a clarifying question rather
                    # than of a sale.
                    f"{name} مقاس {facts['size']} لون {facts['color']}",
                    plan=[
                        [("get_products", {"query": name})],
                        [("get_variants", {"product_id": product_id})],
                        f"تمام، متوفر منه مقاس {facts['size']} بلون {facts['color']} "
                        f"بسعر {facts['price']} جنيه. تحب أحطهولك في السلة؟",
                    ],
                    expects_tools=("get_variants",),
                    expects_photo=True,
                ),
                Step(
                    "أيوه حطهولي",
                    plan=[
                        [("add_to_cart", {"variant_id": variant_id, "quantity": 1})],
                        "اتحط في السلة. تحب تكمل الطلب؟",
                    ],
                    # No `expects_tools`. A model that already put the piece in
                    # the cart on the previous message is right to answer this
                    # without calling anything, and one that adds a second is
                    # also defensible. Both are correct; pinning either would
                    # make the gate fail a good reply.
                ),
            ],
        ),
        Scenario(
            "confirm_order",
            [
                Step(
                    f"{name} مقاس {facts['size']} لون {facts['color']}",
                    plan=[
                        [("get_products", {"query": name})],
                        [("get_variants", {"product_id": product_id})],
                        f"تمام، متوفر منه مقاس {facts['size']} بلون {facts['color']} "
                        f"بسعر {facts['price']} جنيه.",
                    ],
                ),
                Step(
                    "حطهولي في السلة",
                    plan=[
                        [("add_to_cart", {"variant_id": variant_id, "quantity": 1})],
                        "اتحط في السلة. ابعتلي الاسم والعنوان والمحافظة والتليفون.",
                    ],
                ),
                Step(
                    # The address has to agree with the governorate. The first
                    # version of this step said "القاهرة" and then gave an
                    # address in الدقي, which is in Giza -- and the model
                    # correctly stopped and asked which one, because the
                    # shipping fee is per governorate. A scenario that
                    # measures a good question instead of a sale measures the
                    # wrong thing.
                    "أحمد محمد، القاهرة، ٥ شارع التحرير، وسط البلد، ٠١٠٠٠٠٠٠٠٠٠",
                    plan=[
                        [
                            (
                                "confirm_order",
                                {
                                    "customer_name": "أحمد محمد",
                                    "governorate": "Cairo",
                                    "address": "5 شارع التحرير، وسط البلد",
                                    "contact_phone": "01000000000",
                                },
                            )
                        ],
                        "",
                    ],
                    # Also not pinned. Reading the whole order back and asking
                    # the customer to confirm *before* writing it is the flow
                    # this shop wants, and the real model does exactly that --
                    # so requiring `confirm_order` on this step would fail the
                    # correct behaviour.
                ),
            ],
        ),
        Scenario(
            # The half-sleeve question, which the shop got wrong in public:
            # «البولو النص كم» came back as "we have two polos and no published
            # data about sleeve length for either", plus an offer to fetch a
            # person -- about a polo that is on the shelf and is half-sleeve.
            # Pinned here rather than only in a unit test because the failure
            # was never in the lookup: it was the reply reaching for a handoff
            # instead of a field. `quality_gate.dodged_a_sleeve_question` is
            # the rule, this is the conversation it runs on.
            "sleeve_question",
            [
                Step(
                    "عندكم حاجة نص كم؟",
                    plan=[
                        [("get_products", {"sleeve": "half"})],
                        f"أيوه، عندنا كذا قطعة نص كم، منها تيشيرت {name}. تحب تشوف إيه؟",
                    ],
                    expects_tools=("get_products",),
                )
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
                    # No `expects_tools`, and that is the finding rather than
                    # an omission: the shop's shipping fee and delivery window
                    # are in the system prompt, so the real model answers this
                    # in a single hop without entering the tool loop at all.
                    # The one thing the gate has to defend is that it keeps
                    # doing so *and* keeps saying the right number -- a
                    # question with one correct stored answer must not start
                    # costing a round trip, and must not start inventing a fee.
                )
            ],
        ),
    ]


#: The one scenario the target is stated against: "an ordinary reply (a product
#: question with one or two tool calls) under 10 seconds".
ORDINARY = "sizes"
