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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from config.settings import settings

log = logging.getLogger("wanas.dispatcher")


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
    ):
        self._handler = handler
        self._debounce = (
            settings.message_debounce_seconds if debounce_seconds is None else debounce_seconds
        )
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
        if self._debounce <= 0:
            self._run(key, item)
            return

        with self._lock:
            existing = self._pending.get(key)
            if existing is None:
                self._pending[key] = item
                # Counted once per *conversation* that owes a reply, not once
                # per fragment: a rescheduled timer must not leave a second
                # outstanding count that nothing will ever release.
                self._mark_busy()
            else:
                existing.merge(item)

            timer = self._timers.pop(key, None)
            if timer is not None:
                # Still typing. Push the deadline out rather than answering the
                # first fragment of a sentence.
                timer.cancel()

            timer = threading.Timer(self._debounce, self._release, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

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
        with lock:
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
