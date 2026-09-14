"""Adding a piece to an order, and never confusing it with replacing one.

One real conversation, in full:

    customer: «ينفع اضيفه علي نفس الاوردر اللي فات»
    bot:      «طلبك ده لسه محتاج مراجعة من الفريق عشان نضيف عليه قطعة جديدة،
               فبعتلهم الطلب وحد هيأكدلك قريب.»
    queue:    swap_requested -- WNS-1015: swap Knitted Polo (Olive, XL)
                                -> Heart Top (Black, S)

Two failures stacked on one another. The **routing**: `item_swap` was the only
post-order request type that existed, so "add" was forced into the nearest
available shape, and that shape needs something to remove -- so a line was
taken off the existing order to fill the hole. And the **wording**: the reply
said "add" while the queue said "replace", so the customer's own record of the
conversation contradicted the row a staff member was about to act on.

Both halves are pinned here, in both directions: an add must file an add, a
swap must still file a swap with the right from/to, and a reply must describe
whichever was filed.
"""

from __future__ import annotations

import pytest

from assistant import order_change_claims
from assistant.tools.base import ToolContext, call_tool, load_all
from domain.models import Order, QueueKind
from domain.services import carts, orders, queues

load_all()

VARIANT_A = "wanas-hoodie-s-olive"
VARIANT_B = "wanas-hoodie-s-black"
CUSTOMER = "201555000222"


@pytest.fixture()
def ctx(cairo_rate, seeded):
    carts.add(seeded, "whatsapp", CUSTOMER, VARIANT_A, 1)
    result = orders.place_order(
        seeded,
        channel="whatsapp",
        external_id=CUSTOMER,
        customer_name="Hazem",
        governorate="Cairo",
        address="1 Test Street",
        contact_phone="01055566677",
    )
    assert "error" not in result, result
    seeded.flush()
    context = ToolContext(session=seeded, channel="whatsapp", external_id=CUSTOMER)
    context.order_id = result["order_id"]
    return context


def _only(session, kind):
    items = queues.open_items(session, kind)
    assert len(items) == 1, f"{kind}: {items}"
    return items[0]


# --------------------------------------------------------------------------
# routing: what the customer asked for is what goes in the queue
# --------------------------------------------------------------------------


def test_an_add_files_an_add_and_names_nothing_to_remove(ctx):
    """The exact production request. Nothing may come off this order."""
    result = call_tool(
        ctx,
        "request_item_add",
        {
            "order_id": ctx.order_id,
            "to_variant_id": VARIANT_B,
            "note": "عايز أضيفه على نفس الأوردر",
        },
    )
    assert result["queued"] is True
    assert result["filed"] == "item_add"

    item = _only(ctx.session, QueueKind.ITEM_ADD.value)
    assert item.reason == "add_requested"
    assert item.queue_id.startswith("ADD-")
    assert item.payload["to_variant_id"] == VARIANT_B
    # The bug in one assertion: there is no such thing here as a line to remove.
    assert "from_variant_id" not in item.payload
    assert not queues.open_items(ctx.session, QueueKind.ITEM_SWAP.value)


def test_an_add_never_produces_a_swap_however_it_is_described(ctx):
    call_tool(ctx, "request_item_add", {"order_id": ctx.order_id, "note": "عايز كمان واحد"})
    assert not queues.open_items(ctx.session, QueueKind.ITEM_SWAP.value)


def test_a_swap_still_files_a_swap_with_the_right_from_and_to(ctx):
    """The reverse case. Giving "add" its own shape must not have cost the
    swap anything -- the line named is still the line that comes off."""
    result = call_tool(
        ctx,
        "request_item_swap",
        {"order_id": ctx.order_id, "from_variant_id": VARIANT_A, "to_variant_id": VARIANT_B},
    )
    assert result["filed"] == "item_swap"
    item = _only(ctx.session, QueueKind.ITEM_SWAP.value)
    assert item.payload["from_variant_id"] == VARIANT_A
    assert item.payload["to_variant_id"] == VARIANT_B
    assert not queues.open_items(ctx.session, QueueKind.ITEM_ADD.value)


def test_a_swap_still_refuses_a_line_that_is_not_on_the_order(ctx):
    result = call_tool(
        ctx,
        "request_item_swap",
        {"order_id": ctx.order_id, "from_variant_id": VARIANT_B, "to_variant_id": VARIANT_A},
    )
    assert result["error"] == "line_not_found"


def test_an_add_may_leave_the_item_unnamed_rather_than_invent_one(ctx):
    """Same rule as the swap's optional replacement: a required field is how a
    model comes to fill one in with something plausible."""
    result = call_tool(
        ctx, "request_item_add", {"order_id": ctx.order_id, "note": "الهودي الأسود"}
    )
    assert result["queued"] is True
    assert _only(ctx.session, QueueKind.ITEM_ADD.value).payload["to_variant_id"] is None


def test_an_add_of_an_unknown_variant_is_refused(ctx):
    result = call_tool(
        ctx, "request_item_add", {"order_id": ctx.order_id, "to_variant_id": "no-such-variant"}
    )
    assert result["error"] == "variant_not_found"


def test_removing_and_changing_quantity_are_neither_of_these(ctx):
    """The two neighbours, checked because the audit asked whether they had
    the same gap. They do not: both are `modify_order_quantity`, both apply
    immediately, and neither writes a review-queue row at all."""
    result = call_tool(
        ctx,
        "modify_order_quantity",
        {"order_id": ctx.order_id, "variant_id": VARIANT_A, "quantity": 2},
    )
    assert "error" not in result, result
    assert not queues.open_items(ctx.session, QueueKind.ITEM_SWAP.value)
    assert not queues.open_items(ctx.session, QueueKind.ITEM_ADD.value)
    assert ctx.session.get(Order, ctx.order_id).items[0].quantity == 2


# --------------------------------------------------------------------------
# wording: the reply describes what was filed, or it is not sent
# --------------------------------------------------------------------------


def test_the_production_sentence_is_caught_against_a_swap():
    """«عشان نضيف عليه قطعة جديدة» beside a swap in the queue."""
    said = "طلبك ده لسه محتاج مراجعة من الفريق عشان نضيف عليه قطعة جديدة، فبعتلهم الطلب."
    assert order_change_claims.mismatch(said, "item_swap")
    # ...and the same sentence beside the add it describes is fine.
    assert order_change_claims.mismatch(said, "item_add") is None


def test_a_swap_word_beside_a_filed_add_is_caught():
    said = "تمام، هنبدّل القطعة دي بالقطعة الجديدة وحد هيأكدلك."
    assert order_change_claims.mismatch(said, "item_add")
    assert order_change_claims.mismatch(said, "item_swap") is None


@pytest.mark.parametrize(
    "said",
    [
        "طلب الإضافة وصل للفريق، وحد هيأكدلك. بدل ما تعمل أوردر جديد.",
        "سجّلنا إنك عايز تضيف القطعة دي بدلاً من أوردر تاني.",
    ],
)
def test_instead_of_is_not_a_swap(said):
    """«بدل ما» / «بدلاً من» is *instead of*, and it is what an add reply says
    most naturally -- the production note carried exactly that phrase. A guard
    that fired here would fire on the one reply that was right."""
    assert order_change_claims.mismatch(said, "item_add") is None


def test_a_reply_describing_both_halves_of_a_swap_is_correct():
    said = "هنشيل الهودي الزيتي ونضيف الأسود مكانه."
    assert order_change_claims.mismatch(said, "item_swap") is None


def test_a_reply_that_describes_neither_is_not_second_guessed():
    said = "تمام، بعتنا طلبك للفريق وحد هيرد عليك قريب."
    assert order_change_claims.mismatch(said, "item_add") is None
    assert order_change_claims.mismatch(said, "item_swap") is None


def test_two_different_filings_in_one_turn_are_not_judged():
    """Guessing which of them the reply is about would be the same class of
    mistake as the one being closed."""
    assert order_change_claims.filed_kind(["item_add", "item_swap"]) is None
    assert order_change_claims.filed_kind([]) is None
    assert order_change_claims.filed_kind(["item_add"]) == "item_add"


# --------------------------------------------------------------------------
# ...and the two joined up, through the real turn.
# --------------------------------------------------------------------------


def test_a_turn_that_files_an_add_and_describes_a_swap_is_not_sent(ctx, seeded):
    """The production turn, reproduced: the tool files an add and the model
    then says the opposite. The queue row is committed and a staff member will
    act on it, so it is the sentence that has to give way."""
    from assistant import agent
    from assistant.providers.base import ModelReply
    from assistant.providers.fake import ScriptedProvider

    wrong = "تمام، هنبدّل القطعة اللي على الأوردر بالجديدة."
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "request_item_add",
                        "arguments": {"order_id": ctx.order_id, "to_variant_id": VARIANT_B},
                    }
                ]
            ),
            ModelReply(text=wrong),
            ModelReply(text=wrong),
            ModelReply(text=wrong),
        ]
    )
    reply = agent.run_turn(seeded, "whatsapp", CUSTOMER, "ينفع اضيفه على نفس الاوردر", provider=provider)

    assert reply.text != wrong
    assert reply.text == agent.CHANGE_FALLBACK
    assert reply.error == "order_change_mismatch"
    assert reply.filed == ["item_add"]
    # The row is still there: it was right, and only the words were wrong.
    assert len(queues.open_items(seeded, QueueKind.ITEM_ADD.value)) == 1


def test_a_turn_whose_words_match_what_it_filed_goes_out_unchanged(ctx, seeded):
    from assistant import agent
    from assistant.providers.base import ModelReply
    from assistant.providers.fake import ScriptedProvider

    right = "تمام، سجّلنا إننا نضيف القطعة دي على الأوردر وحد هيأكدلك."
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "request_item_add",
                        "arguments": {"order_id": ctx.order_id, "to_variant_id": VARIANT_B},
                    }
                ]
            ),
            ModelReply(text=right),
        ]
    )
    reply = agent.run_turn(seeded, "whatsapp", CUSTOMER, "ينفع اضيفه على نفس الاوردر", provider=provider)
    assert reply.text == right
    assert reply.error is None
