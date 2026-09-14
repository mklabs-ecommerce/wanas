"""Where one turn's wall-clock time actually went.

The bot took between twenty-one seconds and a full minute to answer, and
nothing in the process could say which part of that was the model, which was
Shopify, which was the debounce window and which was our own code. Every
number anyone had was a guess read off a stopwatch on a phone. This module is
the instrument that replaced the guessing.

**One line per turn, and only at the end of it.** A stage is timed into an
in-memory record that lives for the turn; the record is serialised once, as a
single line of JSON, when the turn closes. Logging each stage as it finished
would have been simpler and useless: a turn's stages interleave with seven
other conversations' on the worker pool, so they have to be collected against
the turn they belong to before anything can be counted.

**Nothing in the line is personal.** No message text, no phone number, no
Instagram handle -- the customer is a short hash of their external_id, which
is enough to see that one conversation was slow twice and not enough to say
whose it was. The stage names, the durations, the token counts and the
upstream provider are all the line carries. That constraint is what makes it
safe to leave on in production, which is the only place the real numbers are.

**Nothing here may fail a turn.** Every entry point is a no-op when no turn is
open (a script, the dashboard, a test) and swallows its own errors: an
instrument that can break the thing it measures is worse than no instrument.

Concurrency: the current turn is a `ContextVar`, so the eight dispatcher
worker threads each see their own. A thread starts with a fresh context, which
is exactly the isolation wanted -- the same reason
`integrations/shopify/catalog.py` holds its per-turn snapshot the same way.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from config.settings import settings

log = logging.getLogger("wanas.latency")

#: The prefix every turn line carries, so a log file can be filtered down to
#: exactly these with a grep that cannot match anything else. `railway logs`
#: interleaves uvicorn's access log with ours, and the report script needs a
#: marker it can trust rather than "lines that happen to parse as JSON".
MARKER = "wanas.turn "


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


def customer_hash(external_id: str | None) -> str:
    """A stable, short, non-reversible name for one conversation.

    Eight hex characters of SHA-256. Enough to group a customer's turns
    together while reading a log; not enough to recover the number, which is
    the whole point -- a phone number in a log line is a phone number in every
    place that log is later copied to.
    """
    if not external_id:
        return "anon"
    return hashlib.sha256(str(external_id).encode("utf-8")).hexdigest()[:8]


@dataclass
class Turn:
    """One inbound message, from the webhook to the last byte sent."""

    channel: str = ""
    customer: str = "anon"
    started: float = field(default_factory=time.perf_counter)
    #: name -> [total seconds, occurrences]. Aggregated rather than listed
    #: because a stage that runs twice is still one stage; the per-occurrence
    #: detail that matters (which tool, which model hop) has its own list.
    stages: dict[str, list] = field(default_factory=dict)
    #: One entry per model round trip, in order.
    llm: list[dict] = field(default_factory=list)
    #: One entry per tool call, in order.
    tools: list[dict] = field(default_factory=list)
    #: One entry per Shopify GraphQL call, in order.
    shopify: list[dict] = field(default_factory=list)
    #: Anything the caller wants on the line that is not a duration.
    fields: dict = field(default_factory=dict)
    #: Time that had already passed before this record existed: the webhook's
    #: signature check and transcript write, and the debounce window. They
    #: happen on another thread, minutes of wall clock before the turn opens,
    #: and the customer waited every millisecond of them -- so they are part
    #: of the total even though no clock inside this object measured them.
    #: Kept separately from `stages` because `total_ms` has to add them and
    #: the "what is unattributed" arithmetic has to not count them twice.
    preamble: float = 0.0

    def add(self, name: str, seconds: float) -> None:
        slot = self.stages.get(name)
        if slot is None:
            self.stages[name] = [seconds, 1]
        else:
            slot[0] += seconds
            slot[1] += 1

    def line(self) -> dict:
        answered = time.perf_counter() - self.started
        stages = {name: _ms(total_s) for name, (total_s, _) in self.stages.items()}
        counts = {name: n for name, (_, n) in self.stages.items() if n > 1}
        payload: dict = {
            "ch": self.channel,
            "cust": self.customer,
            # What the customer waited, end to end: the work the webhook did
            # and the debounce window, plus everything this scope timed.
            #
            # `total_ms` used to be the scope alone, and that was wrong in the
            # direction that flatters: the first real production line reported
            # 5883 ms for a turn whose own stages summed to 6942, because the
            # 1000 ms the customer spent in the debounce window and the 82 ms
            # spent writing their message down had both finished before the
            # scope opened. The report noticed before anyone else did -- it
            # put `unattributed` at **-13.6%**, and a negative share is an
            # accounting error by construction.
            "total_ms": _ms(self.preamble + answered),
            # The half this process can still be answering during, kept so the
            # two are never conflated again.
            "reply_ms": _ms(answered),
            "stages": stages,
        }
        if counts:
            payload["stage_n"] = counts
        if self.llm:
            payload["llm"] = self.llm
            payload["hops"] = len(self.llm)
        if self.tools:
            payload["tools"] = self.tools
        if self.shopify:
            payload["shopify"] = self.shopify
        payload.update(self.fields)
        return payload


_current: contextvars.ContextVar[Turn | None] = contextvars.ContextVar(
    "wanas_turn_telemetry", default=None
)


def current() -> Turn | None:
    """The turn being timed on this thread, or None outside one."""
    return _current.get()


@contextmanager
def turn(channel: str, external_id: str, **fields) -> Iterator[Turn | None]:
    """Time one whole turn and emit its line when it closes.

    Yields None when `LATENCY_LOG` is off, so a caller can skip work it only
    does for the instrument. Everything else here already no-ops without a
    turn, so a caller that ignores the yielded value stays correct either way.
    """
    if not settings.latency_log:
        yield None
        return

    record = Turn(channel=channel, customer=customer_hash(external_id), fields=dict(fields))
    token = _current.set(record)
    try:
        yield record
    except BaseException as exc:  # noqa: BLE001 - re-raised immediately below
        record.fields.setdefault("error", type(exc).__name__)
        raise
    finally:
        _current.reset(token)
        try:
            log.info("%s%s", MARKER, json.dumps(record.line(), ensure_ascii=False))
        except Exception:  # pragma: no cover - an instrument may never raise
            log.debug("could not serialise the turn timing line", exc_info=True)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Time one named stage into the open turn. A no-op outside one."""
    record = _current.get()
    if record is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        record.add(name, time.perf_counter() - started)


def add(name: str, seconds: float) -> None:
    """Record a duration measured somewhere this module could not wrap."""
    record = _current.get()
    if record is not None:
        record.add(name, max(0.0, seconds))


def add_before(name: str, seconds: float) -> None:
    """Record a duration the customer waited *before* this turn opened.

    The webhook's work and the debounce window both finish on another thread
    before the turn starts, so a scope-relative total silently leaves them
    out. Counted into `total_ms` as well as named as a stage -- which is the
    difference between "the reply took 5.9 seconds" and "the customer waited
    7.0 seconds", and the customer is the one being measured.
    """
    record = _current.get()
    if record is not None:
        seconds = max(0.0, seconds)
        record.add(name, seconds)
        record.preamble += seconds


def note(**fields) -> None:
    """Put non-duration facts on the turn's line (hop counts, flags, errors)."""
    record = _current.get()
    if record is not None:
        record.fields.update(fields)


@contextmanager
def llm_hop() -> Iterator[None]:
    """Time one model round trip.

    The duration is measured here, in `assistant/agent.py`, so every provider
    is counted the same way -- including the fake one, which is what makes the
    "our code with the model taken out" benchmark comparable. What only the
    real provider knows (tokens, which upstream served it) is attached from
    inside it with `note_llm`, onto the entry this opened.
    """
    record = _current.get()
    if record is None:
        yield
        return
    entry: dict = {}
    record.llm.append(entry)
    started = time.perf_counter()
    try:
        yield
    finally:
        entry["ms"] = _ms(time.perf_counter() - started)
        record.add("llm", time.perf_counter() - started)


def note_llm(**fields) -> None:
    """Attach provider-side detail to the model hop currently being timed.

    Called from the provider, which is the only layer that can read the usage
    payload and the `provider` field OpenRouter returns saying which upstream
    stack actually served the request. Silently does nothing when no hop is
    open, so a provider called outside a turn (the comment classifier, a
    script) stays exactly as it was.
    """
    record = _current.get()
    if record is None or not record.llm:
        return
    record.llm[-1].update({k: v for k, v in fields.items() if v is not None})


@contextmanager
def tool_call(name: str) -> Iterator[None]:
    """Time one tool call, by name."""
    record = _current.get()
    if record is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        record.tools.append({"name": name, "ms": _ms(elapsed)})
        record.add("tools", elapsed)


@contextmanager
def shopify_call(operation: str) -> Iterator[None]:
    """Time one Shopify GraphQL round trip, by operation name."""
    record = _current.get()
    if record is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        record.shopify.append({"op": operation, "ms": _ms(elapsed)})
        record.add("shopify", elapsed)
