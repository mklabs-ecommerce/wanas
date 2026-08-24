"""Picking the governorate instead of typing it.

`AGENTS.md`: "المحافظة اختيار من قايمة، مش نص حر — هي اللي بتحدد سعر الشحن."
Until there was a list message to tap, that rule was aspirational: the model
asked in prose and `shipping.resolve` did its best with aliases. What it costs
when the parse is wrong is a shipping fee quoted for the wrong governorate, or
a parcel that never arrives.

Twenty-seven governorates do not fit in one WhatsApp list -- ten rows is the
limit -- so the picker is two steps, region then governorate. The limits are
the reason for the design, so they are tested as such.
"""

from __future__ import annotations

import pytest

from chatbot import interactive
from chatbot.tools.base import ToolContext, call_tool, load_all
from domain.services import shipping
from domain.services.notifications import LogSender

load_all()

WHO = "201555777888"


@pytest.fixture()
def ctx(seeded):
    return ToolContext(session=seeded, channel="whatsapp", external_id=WHO)


# --- the regions cover the country ---------------------------------------


def test_every_governorate_belongs_to_exactly_one_region(seeded):
    grouped = [key for _id, _label, members in shipping.REGIONS for key in members]
    assert sorted(grouped) == sorted(shipping.valid_governorates(seeded))
    assert len(grouped) == len(set(grouped)), "a governorate is in two regions"


def test_no_step_of_the_picker_exceeds_whatsapp_s_row_limit():
    """The whole reason it is two steps and not one."""
    assert len(shipping.REGIONS) <= interactive.MAX_ROWS
    for _id, label, members in shipping.REGIONS:
        assert len(members) <= interactive.MAX_ROWS, label


# --- the tool -------------------------------------------------------------


def test_the_first_call_offers_the_regions(ctx):
    result = call_tool(ctx, "ask_governorate", {})

    assert result["step"] == "region"
    assert len(result["regions"]) == len(shipping.REGIONS)
    assert ctx.interactive["kind"] == "list"
    titles = [row["title"] for row in ctx.interactive["sections"][0]["rows"]]
    assert "القاهرة الكبرى" in titles


def test_picking_a_region_offers_its_governorates(ctx):
    result = call_tool(ctx, "ask_governorate", {"region": "الدلتا"})

    assert result["step"] == "governorate"
    assert result["region"] == "delta"
    assert {row["governorate"] for row in result["governorates"]} == {
        "Sharqia",
        "Dakahlia",
        "Gharbia",
        "Monufia",
        "Kafr El Sheikh",
        "Damietta",
    }
    # The customer sees Arabic; the id carried back is the stored key.
    rows = ctx.interactive["sections"][0]["rows"]
    assert {row["id"] for row in rows} >= {"Dakahlia", "Damietta"}
    assert any(row["title"] == "الدقهلية" for row in rows)


def test_a_region_id_works_as_well_as_its_label(ctx):
    assert call_tool(ctx, "ask_governorate", {"region": "greater_cairo"})["region"] == "greater_cairo"


def test_a_customer_who_just_names_their_governorate_skips_a_step(ctx):
    """"أنا من طنطا" is an answer, not a region. Do not make them start over."""
    result = call_tool(ctx, "ask_governorate", {"region": "طنطا"})

    assert result == {"step": "done", "governorate": "Gharbia"}
    assert ctx.interactive is None


def test_nonsense_is_refused_with_the_regions_to_re_ask_with(ctx):
    result = call_tool(ctx, "ask_governorate", {"region": "كوكب المريخ"})

    assert result["error"] == "unknown_region"
    assert "delta" in result["regions"]


def test_only_one_picker_can_ride_on_a_reply(ctx):
    """A turn is one message; a second picker would silently replace the first."""
    call_tool(ctx, "ask_governorate", {})
    first = ctx.interactive
    call_tool(ctx, "ask_governorate", {"region": "delta"})

    assert ctx.interactive is first


def test_a_governorate_with_no_fee_is_still_offered(ctx):
    """Hiding it would leave the customer hunting; confirm_order refuses it
    with a reason the bot can actually explain."""
    rows = call_tool(ctx, "ask_governorate", {"region": "remote"})["governorates"]
    assert {row["governorate"] for row in rows} == {"Red Sea", "New Valley"}


# --- the payload the adapter builds --------------------------------------


def test_row_titles_are_clipped_rather_than_rejected():
    payload = interactive.list_message("body", "اختار", [{"id": "x", "title": "ا" * 40}])
    assert len(payload["sections"][0]["rows"][0]["title"]) <= interactive.MAX_TITLE


def test_more_rows_than_whatsapp_allows_are_truncated():
    rows = [{"id": str(index), "title": str(index)} for index in range(25)]
    payload = interactive.list_message("body", "اختار", rows)
    assert len(payload["sections"][0]["rows"]) == interactive.MAX_ROWS


def test_the_whatsapp_client_translates_a_list_into_meta_s_shape():
    from integrations.whatsapp.client import WhatsAppClient

    built = WhatsAppClient._interactive_payload(
        interactive.region_picker(shipping.regions())
    )
    assert built["type"] == "list"
    assert built["action"]["button"] == "اختار"
    assert built["action"]["sections"][0]["rows"][0]["id"] == "greater_cairo"


def test_an_unknown_kind_falls_back_to_text_rather_than_sending_nothing():
    from integrations.whatsapp.client import WhatsAppClient

    assert WhatsAppClient._interactive_payload({"kind": "carousel"}) is None


def test_buttons_are_capped_at_three():
    payload = interactive.buttons_message(
        "أكد؟", [{"id": str(n), "title": str(n)} for n in range(6)]
    )
    assert len(payload["buttons"]) == interactive.MAX_BUTTONS


def test_the_log_sender_records_an_interactive_message():
    """So the harness and the tests can see one without a Meta credential."""
    sender = LogSender()
    sender.send_interactive("2010", {"kind": "list", "body": "اختار المنطقة"})
    assert sender.sent[0].kind == "interactive"
    assert sender.sent[0].text == "اختار المنطقة"
