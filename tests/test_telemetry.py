"""The turn timing line: what it says, and what it must never say.

Two things are being defended here. The first is that the instrument works --
stages add up, a repeated stage is counted once with its total, the model hops
and the tool calls keep their order. The second is the part that lets it stay
switched on in production: the line carries no message text and no customer
identifier, only a hash, and nothing in it may raise. An instrument that can
fail a turn is worse than no instrument, so "no turn open" and "unserialisable
field" are both tested as ordinary, silent cases.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time

from common import telemetry
from config.settings import settings


def _lines(caplog) -> list[dict]:
    return [
        json.loads(record.getMessage().removeprefix(telemetry.MARKER))
        for record in caplog.records
        if record.getMessage().startswith(telemetry.MARKER)
    ]


def test_a_turn_emits_one_line_naming_every_stage(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn(
        "whatsapp", "201000000000"
    ):
        with telemetry.stage("history_load"):
            pass
        with telemetry.stage("context_build"):
            pass

    (line,) = _lines(caplog)
    assert line["ch"] == "whatsapp"
    assert set(line["stages"]) == {"history_load", "context_build"}
    assert line["total_ms"] >= 0


def test_the_customer_is_a_hash_and_never_the_number(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn(
        "whatsapp", "201234567890"
    ):
        pass

    (line,) = _lines(caplog)
    assert "201234567890" not in json.dumps(line)
    assert line["cust"] == telemetry.customer_hash("201234567890")
    assert len(line["cust"]) == 8
    # Same customer, same name -- otherwise two slow turns from one number
    # cannot be seen as one number having a bad time.
    assert telemetry.customer_hash("201234567890") == telemetry.customer_hash("201234567890")
    assert telemetry.customer_hash("201234567890") != telemetry.customer_hash("201234567891")


def test_a_repeated_stage_is_summed_and_counted(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x"):
        telemetry.add("llm", 1.0)
        telemetry.add("llm", 0.5)

    (line,) = _lines(caplog)
    assert line["stages"]["llm"] == 1500.0
    assert line["stage_n"]["llm"] == 2


def test_model_hops_keep_their_order_and_their_provider_detail(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x"):
        with telemetry.llm_hop():
            telemetry.note_llm(upstream="novita", prompt_tokens=7000, reasoning_tokens=120)
        with telemetry.llm_hop():
            telemetry.note_llm(upstream="deepinfra", prompt_tokens=9000)

    (line,) = _lines(caplog)
    assert line["hops"] == 2
    assert [hop["upstream"] for hop in line["llm"]] == ["novita", "deepinfra"]
    assert line["llm"][0]["reasoning_tokens"] == 120
    # A field the provider did not return is left off rather than written as
    # null -- a missing count and a count of zero are different facts.
    assert "reasoning_tokens" not in line["llm"][1]


def test_tools_and_shopify_calls_are_named_individually(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x"):
        with telemetry.tool_call("get_products"):
            pass
        with telemetry.tool_call("get_variants"):
            pass
        with telemetry.shopify_call("productVariants"):
            pass

    (line,) = _lines(caplog)
    assert [t["name"] for t in line["tools"]] == ["get_products", "get_variants"]
    assert [c["op"] for c in line["shopify"]] == ["productVariants"]


def test_notes_land_on_the_line(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x", batch=2):
        telemetry.note(tool_calls=["get_products"], turn_error=None)

    (line,) = _lines(caplog)
    assert line["batch"] == 2
    assert line["tool_calls"] == ["get_products"]


def test_a_crashing_turn_still_reports_and_still_raises(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"):
        try:
            with telemetry.turn("whatsapp", "x"):
                raise ValueError("boom")
        except ValueError:
            pass

    (line,) = _lines(caplog)
    assert line["error"] == "ValueError"


def test_everything_is_a_no_op_outside_a_turn(caplog):
    """A script, the dashboard, a test -- none of them open a turn, and none
    of them may be broken by code that assumes one."""
    with caplog.at_level(logging.INFO, logger="wanas.latency"):
        with telemetry.stage("history_load"):
            pass
        with telemetry.llm_hop():
            telemetry.note_llm(upstream="novita")
        with telemetry.tool_call("get_products"):
            pass
        with telemetry.shopify_call("productVariants"):
            pass
        telemetry.add("llm", 1.0)
        telemetry.note(anything=True)

    assert telemetry.current() is None
    assert _lines(caplog) == []


def test_an_unserialisable_field_costs_the_line_and_nothing_else(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x"):
        telemetry.note(oops=object())

    assert _lines(caplog) == []


def test_the_flag_switches_it_off(caplog, monkeypatch):
    monkeypatch.setattr(telemetry, "settings", dataclasses.replace(settings, latency_log=False))
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x") as record:
        assert record is None
        with telemetry.stage("history_load"):
            pass

    assert _lines(caplog) == []


# --------------------------------------------------------------------------
# what the customer waited, as opposed to what the reply took
# --------------------------------------------------------------------------
#
# The debounce window and the webhook's own work finish on another thread
# before the turn scope opens. Counting them as ordinary stages made
# `total_ms` smaller than the stages it contained -- the first real production
# line reported 5883 ms for a turn whose stages summed to 6942 -- and the
# report said so by putting `unattributed` below zero.


def test_the_total_includes_what_happened_before_the_turn_opened(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x"):
        telemetry.add_before("debounce_wait", 1.0)
        telemetry.add_before("record_inbound", 0.08)
        telemetry.add("llm", 5.0)

    (line,) = _lines(caplog)
    # The wait is in the total...
    assert line["total_ms"] >= 1080.0
    # ...and named as a stage as well, so the report can show where it went.
    assert line["stages"]["debounce_wait"] == 1000.0
    assert line["stages"]["record_inbound"] == 80.0


def test_the_reply_half_is_reported_separately(caplog):
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x"):
        telemetry.add_before("debounce_wait", 1.0)

    (line,) = _lines(caplog)
    # `reply_ms` is the scope alone and must not have grown by the wait; the
    # two are never to be conflated again.
    assert line["reply_ms"] < 500.0
    assert line["total_ms"] >= 1000.0
    assert line["total_ms"] > line["reply_ms"]


def test_no_stage_can_sum_past_the_total(caplog):
    """The invariant the negative `unattributed` violated: every stage the turn
    recorded has to fit inside what the customer waited.

    Timed with real sleeps rather than asserted durations, because that is how
    a turn actually accrues them -- a stage the scope did not really spend
    time in would fail this for a reason production never has.
    """
    with caplog.at_level(logging.INFO, logger="wanas.latency"), telemetry.turn("whatsapp", "x"):
        telemetry.add_before("debounce_wait", 1.0)
        telemetry.add_before("record_inbound", 0.05)
        with telemetry.stage("history_load"):
            time.sleep(0.01)
        with telemetry.llm_hop():
            time.sleep(0.02)
        with telemetry.stage("send"):
            time.sleep(0.01)

    (line,) = _lines(caplog)
    assert sum(line["stages"].values()) <= line["total_ms"] + 1.0
    # And the wait really is the bulk of it, which is the point of counting it.
    assert line["stages"]["debounce_wait"] > line["reply_ms"]


def test_add_before_is_a_no_op_outside_a_turn():
    telemetry.add_before("debounce_wait", 1.0)
    assert telemetry.current() is None
