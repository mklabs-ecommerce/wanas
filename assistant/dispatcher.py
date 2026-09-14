"""Getting the work out of the webhook, and merging what arrives together.

Two problems, one mechanism.

**The webhook used to do the work.** `POST /webhooks/whatsapp` parsed the
message and then ran the whole agent turn -- a model call that can take thirty
seconds, sometimes a Shopify read on top -- before returning 200. The endpoint
is `async`, the turn is not, so a single customer's message blocked the event
loop and with it every other conversation the process was handling. Meta also
retries a delivery it has not been acknowledged, and a webhook that keeps
timing out eventually gets switched off.

**Customers type in fragments.** "عايز هودي" / "أسود" / "لارج" inside five
seconds is one request. Answered one message at a time it costs three model
calls and produces three replies that read like three different people.

So: the webhook hands each message here and returns immediately. Messages for
the same conversation are collected for `MESSAGE_DEBOUNCE_SECONDS`, then run
**once**, joined into a single turn, on a worker thread.

Scope, stated plainly: this is in-process. It fits one Railway instance, which
is what this runs on. Two instances would debounce independently -- correct,
just less effective -- and a restart loses whatever was still buffered, which
is why the idempotency claim is taken at ingest and not here. Moving to Redis
later means replacing this file, not its callers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from config.settings import settings

log = logging.getLogger("wanas.dispatcher")

#: How many conversations the fragment memory holds before it starts pruning.
#: Comfortably above this shop's whole customer list; the cap is there so the
#: dict cannot grow without bound on a process that runs for weeks, not
#: because anyone expects to reach it.
_FRAGMENTER_MEMORY = 5000


@dataclass
class Pending:
    """What has arrived for one conversation and not been answered yet."""

    texts: list[str] = field(default_factory=list)
    #: Parallel to `texts` -- the message id each fragment arrived as, so a
    #: later reply-to reference in this same batch can be resolved back to
    #: which text it was replying to. See `reply_to` and `annotated_text`.
    text_ids: list[str | None] = field(default_factory=list)
    image_paths: list[str] = field(default_factory=list)
    image_ids: list[str | None] = field(default_factory=list)
    audio_paths: list[str] = field(default_factory=list)
    audio_ids: list[str | None] = field(default_factory=list)
    #: The last inbound message id, so the adapter can mark the right one read.
    last_message_id: str | None = None
    #: message id -> the id of the WhatsApp message it was a reply to (Meta's
    #: `context.id`). Resolved here when the quoted message is in this same
    #: debounced batch; anything else is handed to `assistant/quoting.py`,
    #: which looks it up in the stored transcript -- see
    #: `unresolved_reply_to`.
    reply_to: dict[str, str] = field(default_factory=dict)
    #: Platform message ids already written to the transcript on arrival
    #: (`assistant/runtime.py::record_inbound`). The turn folds those
    #: provisional copies into the single message it stores, so the
    #: conversation is visible immediately without reading twice afterwards.
    recorded_ids: set[str] = field(default_factory=set)
    extras: dict = field(default_factory=dict)
    #: What the webhook spent on this message before it was queued, by stage
    #: name, in seconds -- signature check, media download, `record_inbound`,
    #: the read receipt. Measured in the adapter and carried here because the
    #: turn it belongs to runs on another thread minutes of wall clock later,
    #: and the one line per turn has to be able to say so. Summed across a
    #: merged batch: three fragments cost three ingests.
    ingest: dict[str, float] = field(default_factory=dict)
    #: `time.perf_counter()` when the first and the most recent fragment of
    #: this batch arrived. The difference between `last_seen` and the moment
    #: the handler starts is the debounce wait the customer actually paid,
    #: which is not the same as the configured window once a second message
    #: has pushed the deadline out.
    first_seen: float = 0.0
    last_seen: float = 0.0
    #: How many platform messages this batch has collected. 1 is the ordinary
    #: case by a very long way -- 249 of 254 measured turns -- and it is what
    #: the adaptive window below keys on.
    fragments: int = 0

    def spent(self, name: str, seconds: float) -> None:
        """Note ingest-side time against this message."""
        self.ingest[name] = self.ingest.get(name, 0.0) + max(0.0, seconds)

    def merge(self, other: Pending) -> None:
        self.texts.extend(other.texts)
        self.text_ids.extend(other.text_ids)
        self.image_paths.extend(other.image_paths)
        self.image_ids.extend(other.image_ids)
        self.audio_paths.extend(other.audio_paths)
        self.audio_ids.extend(other.audio_ids)
        self.last_message_id = other.last_message_id or self.last_message_id
        self.reply_to.update(other.reply_to)
        self.recorded_ids |= other.recorded_ids
        self.extras.update(other.extras)
        for name, seconds in other.ingest.items():
            self.ingest[name] = self.ingest.get(name, 0.0) + seconds
        self.first_seen = min(t for t in (self.first_seen, other.first_seen) if t) or 0.0
        self.last_seen = max(self.last_seen, other.last_seen)
        self.fragments += other.fragments

    @property
    def text(self) -> str:
        """The fragments as one message.

        Newline-joined rather than space-joined: "عايز هودي" and "أسود" are two
        sentences, and running them together reads as one garbled one.
        """
        return "\n".join(part for part in self.texts if part.strip())

    def _batch_labels(self) -> dict[str, str]:
        """What each message *in this batch* can be called, by its id.

        Text fragments are named too, not only media: a customer who sends
        three lines and then replies to the first of them was previously left
        with no annotation at all, since only photos and voice notes had a
        label to point at.
        """
        labels: dict[str, str] = {}
        for index, mid in enumerate(self.image_ids, start=1):
            if mid:
                labels[mid] = f"photo {index}"
        for index, mid in enumerate(self.audio_ids, start=1):
            if mid:
                labels[mid] = f"voice note {index}"
        for index, mid in enumerate(self.text_ids, start=1):
            if mid and mid not in labels:
                text = self.texts[index - 1] if index - 1 < len(self.texts) else ""
                snippet = " ".join((text or "").split())
                if len(snippet) > 80:
                    snippet = snippet[:80].rstrip() + "…"
                labels[mid] = f'their message "{snippet}"' if snippet else f"message {index}"
        return labels

    def unresolved_reply_to(self) -> list[str]:
        """Quoted ids this batch cannot explain on its own.

        A reply to something the bot said, or to a message from a turn that
        has already been answered, points outside the debounce window -- which
        is most of them. They go to the runtime, which resolves them against
        the stored transcript (`assistant/quoting.py`). Anything the batch
        *can* label is left out, so the same quote is never described twice.
        """
        labels = self._batch_labels()
        return [target for target in self.reply_to.values() if target and target not in labels]

    def annotated_text(self) -> str:
        """The batch's text fragments, each prefixed with which earlier item in
        this same batch it was a WhatsApp "reply to" of, when Meta says so.

        Needed because the neutral message format (`assistant/messages.py`) only
        ever carries plain text -- there is no structured "in reply to" field to
        thread through the provider boundary, so this is where "the customer
        replied to the second photo" becomes words the model can actually use:
        `[replying to photo 2] a size M please`, folded straight into the text
        the turn runs on.
        """
        if not self.reply_to:
            return self.text

        labels = self._batch_labels()

        out = []
        for index, text in enumerate(self.texts):
            mid = self.text_ids[index] if index < len(self.text_ids) else None
            target = self.reply_to.get(mid) if mid else None
            label = labels.get(target) if target else None
            annotated = f"[replying to {label}] {text}" if label else text
            if annotated.strip():
                out.append(annotated)
        return "\n".join(out)


class MessageDispatcher:
    """Debounce per conversation, then run the handler on a worker thread.

    Threads rather than asyncio on purpose. The handler is synchronous all the
    way down -- SQLAlchemy, httpx, the agent loop -- so an asyncio version would
    hand it to an executor anyway, and this way the class behaves identically
    under uvicorn, under the test client, and when called from a plain script.
    """

    def __init__(
        self,
        handler: Callable[[str, Pending], None],
        *,
        debounce_seconds: float | None = None,
        max_workers: int | None = None,
        adaptive: bool | None = None,
        first_debounce_seconds: float | None = None,
        max_batch_seconds: float | None = None,
    ):
        self._handler = handler
        self._debounce = (
            settings.message_debounce_seconds if debounce_seconds is None else debounce_seconds
        )
        self._adaptive = settings.adaptive_debounce if adaptive is None else adaptive
        self._first_debounce = (
            settings.message_debounce_first_seconds
            if first_debounce_seconds is None
            else first_debounce_seconds
        )
        self._max_batch = (
            settings.message_debounce_max_seconds
            if max_batch_seconds is None
            else max_batch_seconds
        )
        #: Conversations that have been seen writing in fragments, and when.
        #: They get the long window from their *first* message rather than
        #: having to prove it again every time -- see `_wait_for`. Bounded and
        #: pruned by age, because an unbounded dict keyed on customer is the
        #: same slow leak `_release_conversation_lock` exists to stop.
        self._fragmenters: dict[str, float] = {}
        #: When each conversation's last batch was handed to the handler, so a
        #: message arriving hard on the heels of one can be recognised as the
        #: rest of a thought rather than a new one.
        self._last_release: dict[str, float] = {}
        self._pending: dict[str, Pending] = {}
        self._timers: dict[str, threading.Timer] = {}
        #: One conversation is answered one message at a time. Without this a
        #: fragment that arrives while the previous turn is mid-flight produces
        #: two agent turns racing over the same session row.
        self._conversation_locks: dict[str, threading.Lock] = {}
        #: How many threads hold or are waiting on each conversation lock, so
        #: the entry can be dropped when the count reaches zero instead of
        #: accumulating one lock per customer forever. See
        #: `_release_conversation_lock`.
        self._lock_users: dict[str, int] = {}
        # Re-entrant: `submit` takes it and then updates the busy counter,
        # which takes it again on the same thread.
        self._lock = threading.RLock()
        self._inflight = 0
        self._idle = threading.Event()
        self._idle.set()
        self._pool = ThreadPoolExecutor(
            max_workers=settings.message_workers if max_workers is None else max_workers,
            thread_name_prefix="wanas-msg",
        )

    # -- submission -------------------------------------------------------

    def submit(self, key: str, item: Pending) -> None:
        """Queue a message. Returns immediately, always.

        With debounce at zero the handler runs inline, in the caller's thread.
        That is what the test suite wants -- assert on the outcome right after
        the request -- and it is never what production wants.
        """
        # Stamped here rather than in the adapter so every channel gets it for
        # free, and so the clock that measures the wait is the same one that
        # ends it. `first_seen` survives a merge; `last_seen` is what the wait
        # is actually measured from, because a second fragment pushes the
        # deadline out and the customer pays from *their last* message.
        now = time.perf_counter()
        if not item.first_seen:
            item.first_seen = now
        item.last_seen = now
        item.fragments = max(1, item.fragments)

        if self._debounce <= 0:
            self._run(key, item)
            return

        with self._lock:
            existing = self._pending.get(key)
            if existing is None:
                # Nothing buffered, so this opens a batch. If the previous one
                # was released moments ago, this message is almost certainly
                # the rest of what they were saying and the window closed too
                # early on them -- remember that, so it does not happen to
                # this customer again.
                released = self._last_release.get(key)
                if released is not None and now - released <= self._fragment_memory_window:
                    self._note_fragmenter(key, now)
                self._pending[key] = item
                # Counted once per *conversation* that owes a reply, not once
                # per fragment: a rescheduled timer must not leave a second
                # outstanding count that nothing will ever release.
                self._mark_busy()
            else:
                # A second message inside the window: this conversation writes
                # in fragments, and every later batch of theirs should start
                # patient rather than learn it again.
                existing.merge(item)
                self._note_fragmenter(key, now)

            timer = self._timers.pop(key, None)
            if timer is not None:
                # Still typing. Push the deadline out rather than answering the
                # first fragment of a sentence.
                timer.cancel()

            pending = self._pending[key]
            timer = threading.Timer(self._wait_for(pending, key), self._release, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    @property
    def _fragment_memory_window(self) -> float:
        """How soon after a batch was released a new message still counts as
        the rest of the same thought.

        Deliberately generous: being wrong in this direction costs one
        customer a few seconds of patience they did not need, and being wrong
        in the other direction is the split reply this whole mechanism exists
        to avoid.
        """
        return max(self._debounce, settings.message_fragment_memory_seconds)

    def _note_fragmenter(self, key: str, now: float) -> None:
        """Remember that this conversation writes in pieces.

        Pruned by age and capped, because a dict keyed on customer that only
        ever grows is the same invisible leak `_release_conversation_lock`
        exists to stop -- slow, unbounded, and only ever noticed as a process
        that grows for weeks. Called with `self._lock` held.
        """
        self._fragmenters[key] = now
        if len(self._fragmenters) <= _FRAGMENTER_MEMORY:
            return
        cutoff = now - settings.message_fragment_memory_ttl_seconds
        self._fragmenters = {k: t for k, t in self._fragmenters.items() if t > cutoff}
        if len(self._fragmenters) > _FRAGMENTER_MEMORY:
            # Still too many even after the age prune: keep the most recent.
            keep = sorted(self._fragmenters.items(), key=lambda kv: -kv[1])[:_FRAGMENTER_MEMORY]
            self._fragmenters = dict(keep)

    def _knows_fragmenter(self, key: str, now: float) -> bool:
        seen = self._fragmenters.get(key)
        return seen is not None and now - seen <= settings.message_fragment_memory_ttl_seconds

    def _wait_for(self, item: Pending, key: str = "") -> float:
        """How long this batch waits for the customer's next message.

        The window exists because customers type in fragments, and that is
        still true. What was not true is that it should cost the same on every
        message: 249 of 254 measured production turns were a *single* message,
        so the fixed six seconds was paid in full by 98% of turns to catch the
        other 2%. Six seconds is also 28% of a twenty-one-second reply, which
        made it the second most expensive thing in the whole path and the only
        one nothing outside this process had a say in.

        **And the 2% are not a random 2%.** Writing in fragments is a habit of
        a person, not a property of a message, so a conversation that has done
        it once is remembered and starts patient every time after -- which is
        what lets the default for everyone else be a second rather than the
        two it had to be when the short window was the only thing standing
        between a fragmenting customer and a split reply. The rare case is
        better served than it was *and* the common one is faster.

        So the wait is short until a second fragment proves it is needed, and
        then it is exactly what it always was:

        * one message, from a conversation not known to fragment ->
          `MESSAGE_DEBOUNCE_FIRST_SECONDS` (1 s).
        * one message, from a conversation that has fragmented before, or that
          wrote again within seconds of its last batch being answered ->
          the full window.
        * two or more -> `MESSAGE_DEBOUNCE_SECONDS` (6 s), measured from the
          newest one, which is byte-for-byte the old behaviour. A customer who
          is *actually* writing in pieces gets the same patience they always
          did; the deadline for their second fragment lands at the same wall
          clock second as before.
        * and never past `MESSAGE_DEBOUNCE_MAX_SECONDS` (15 s) from the first
          fragment, so someone typing one word every five seconds cannot hold
          a batch open indefinitely -- which the old fixed window could not do
          either, and which becomes reachable once the window extends.

        The cost is real and worth naming: a customer whose second fragment
        arrives between 2 and 6 seconds after the first now gets two turns
        instead of one. That is an extra model call and a reply that reads as
        two messages rather than one -- not a wrong answer, and the
        conversation lock still serialises them. Against it: four seconds off
        98% of replies. `ADAPTIVE_DEBOUNCE=0` restores the old window exactly.
        """
        if not self._adaptive:
            return self._debounce

        # The short wait is never longer than the full one. Adaptive must not
        # be able to make a single message *slower* than the fixed window it
        # replaced, and a caller that pins a short `debounce_seconds` (the
        # tests do) means that number, not the production default.
        first = min(self._first_debounce, self._debounce)
        patient = item.fragments > 1 or (key and self._knows_fragmenter(key, time.perf_counter()))
        wait = self._debounce if patient else first
        if self._max_batch > 0 and item.first_seen:
            age = time.perf_counter() - item.first_seen
            # Never negative: a batch already past the ceiling runs now rather
            # than scheduling a timer in the past.
            wait = min(wait, max(0.05, self._max_batch - age))
        return max(0.05, wait)

    def _release(self, key: str) -> None:
        with self._lock:
            self._timers.pop(key, None)
            item = self._pending.pop(key, None)
        if item is None:
            self._mark_done()
            return
        # The debounce timer's "busy" is handed to the pool task, not released
        # and re-taken, so `wait_idle` cannot see a gap in the middle.
        self._pool.submit(self._run_and_release, key, item)

    def _run_and_release(self, key: str, item: Pending) -> None:
        try:
            self._run(key, item)
        finally:
            self._mark_done()

    def _run(self, key: str, item: Pending) -> None:
        lock = self._conversation_lock(key)
        queued = time.perf_counter()
        with self._lock:
            # When this conversation was last handed to the handler. A message
            # arriving just after it is the rest of a thought whose window
            # closed early -- see `submit`.
            self._last_release[key] = queued
            if len(self._last_release) > _FRAGMENTER_MEMORY:
                cutoff = queued - self._fragment_memory_window
                self._last_release = {k: t for k, t in self._last_release.items() if t > cutoff}
        with lock:
            # Everything between the customer's last message and the handler
            # starting: the debounce window itself, plus however long this
            # conversation waited behind its own previous turn. Separated
            # because the first is a tuning decision and the second is
            # contention, and they are fixed by completely different things.
            if item.last_seen:
                item.spent("debounce_wait", queued - item.last_seen)
            item.spent("turn_queue_wait", time.perf_counter() - queued)
            try:
                self._handler(key, item)
            except Exception:
                # A dropped message is bad; a dead worker thread is worse,
                # because every later message for every customer is dropped
                # silently after it.
                log.exception("failed to handle buffered messages for %s", key)
            finally:
                self._release_conversation_lock(key, lock)

    def _conversation_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._conversation_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._conversation_locks[key] = lock
            self._lock_users[key] = self._lock_users.get(key, 0) + 1
            return lock

    def _release_conversation_lock(self, key: str, lock: threading.Lock) -> None:
        """Forget a conversation's lock once nothing is holding or waiting for
        it.

        Without this the dict is append-only: one `threading.Lock` per
        customer who has *ever* messaged, kept for the life of the process.
        On a single long-lived Railway instance that is an unbounded leak
        keyed on customer count -- slow, invisible, and only ever noticed as a
        process that grows for weeks.

        Refcounted rather than "delete after the last run", because deleting a
        lock another thread is already blocked on would hand the next arrival
        a *different* lock object for the same conversation -- which is the
        two-turns-racing-one-session-row bug this lock exists to stop.
        """
        with self._lock:
            remaining = self._lock_users.get(key, 1) - 1
            if remaining > 0:
                self._lock_users[key] = remaining
                return
            self._lock_users.pop(key, None)
            # Only drop the entry if it is still the same object -- a lock
            # replaced under us belongs to a newer arrival.
            if self._conversation_locks.get(key) is lock:
                self._conversation_locks.pop(key, None)

    # -- lifecycle --------------------------------------------------------

    def _mark_busy(self) -> None:
        with self._lock:
            self._inflight += 1
            self._idle.clear()

    def _mark_done(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if self._inflight == 0:
                self._idle.set()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until nothing is buffered or running. For tests and shutdown."""
        return self._idle.wait(timeout)

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop taking new work, and answer what is already buffered.

        The flush is the point. `app.py`'s lifespan calls this on every deploy
        to "let anything still buffered finish rather than dropping a
        customer's message" -- but cancelling the timers and shutting the pool
        does not do that on its own: `_pool.shutdown` only waits for tasks
        already *submitted*, and a conversation still inside its debounce
        window has no task yet. It sat in `_pending` and was thrown away, so
        every deploy silently abandoned mid-sentence customers, who had
        already been recorded as having written (`record_inbound`) and so read
        as unanswered forever.

        Cancelling first and draining second is deliberate: a timer that fires
        while we drain finds `_pending` empty and releases its own busy count,
        so nothing is run twice and nothing is left counted.
        """
        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()

        with self._lock:
            buffered = list(self._pending.items())
            self._pending.clear()
        for key, item in buffered:
            try:
                self._pool.submit(self._run_and_release, key, item)
            except RuntimeError:
                # The pool is already shutting down (a second shutdown call).
                # Run it here rather than lose it.
                log.warning("flushing buffered messages for %s inline at shutdown", key)
                self._run_and_release(key, item)

        self._pool.shutdown(wait=wait)

    @property
    def debounce_seconds(self) -> float:
        return self._debounce
