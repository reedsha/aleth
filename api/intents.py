"""The intent queue, and the loop that drains it.

The frontend cannot spawn anything. What it can do is *ask*, and this is where the asking stops
being the frontend's problem: a request that validates is appended here and answered with a
receipt, and the orchestrator takes it from the queue when it is free.

Three properties are the point:

* **Bounded.** A queue with no ceiling is a memory leak with a nice name; a client that submits
  faster than runs complete is refused rather than accumulated.
* **Serialised.** One intent is drained at a time, and since Phase 25 that is enforced by the
  **ledger**, not by this object. ``take`` claims the project's execution slot
  (``IntentLedger.claim``) before it pops anything, so an intent that cannot claim *stays queued at
  the head*: two intents for one project are a wait, not two runs over one working tree. That also
  holds **across processes**, which the in-memory queue never could -- and it is why the mutex
  lives in SQLite rather than in a file lock beside it.
* **Deferrable.** A concurrency condition is not an outcome. When a run cannot be started because
  the engine is busy with something that did not come from this queue, the intent is released back
  to ``queued`` (:class:`RunDeferred`) rather than failed -- failing it would gate the UI on a run
  the user never asked to fail.

The worker is a plain function over the queue, so it can be driven by a thread in the app and by
a test with no thread at all.
"""

from __future__ import annotations

import collections
import dataclasses
import sys
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional

from api.schemas import IntentLedgerEntry, IntentQueueStatus, IntentRequest, IntentView

# A UI that has queued this many unrun intents is not asking for work, it is stuck; refusing the
# next one is the honest answer, and the frontend gets a 429 rather than an ever-growing list.
DEFAULT_CAPACITY = 32


class IntentQueueFull(Exception):
    """The queue is at capacity. A refusal, not a silent drop."""


class RunDeferred(Exception):
    """A run could not be started because the engine is already busy with another one.

    Not a failure: the intent keeps its place in the queue and the slot goes back. Raised by the
    run path and caught by :func:`run_worker`, which releases rather than settles.
    """


@dataclasses.dataclass
class Intent:
    """One accepted request, with the identity the API answers with."""

    id: str
    action_type: str
    message: str
    action_params: Dict[str, object]
    enqueued_at: float
    status: str = "queued"
    error: str = ""

    def view(self) -> IntentView:
        return IntentView(
            intent_id=self.id,
            action_type=self.action_type,
            message=self.message,
            status=self.status,
            error=self.error,
            enqueued_at=self.enqueued_at,
        )


class IntentQueue:
    """A bounded FIFO of validated intents, safe to submit to and drain from any thread.

    ``ledger`` is optional so the queue stays testable without a database, and it is written
    through on every transition: accepted, taken, settled. The ledger, not this object, is what
    survives a crash -- the queue is in memory and dies with the process.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY, ledger: Any = None):
        self._capacity = max(1, int(capacity))
        self._ledger = ledger
        self._items: Deque[Intent] = collections.deque()
        self._history: List[Intent] = []
        self._condition = threading.Condition()
        self._accepted = 0
        self._completed = 0
        self._failed = 0
        # The intent handed out by ``take`` and not yet settled. The authority on "what is
        # running" for a caller that is not on the run's thread -- see ``current``.
        self._current: Optional[Intent] = None

    def _write(self, action: str, intent: Intent) -> None:
        """One ledger transition. A lost write is reported, never swallowed.

        The ledger is what the next boot reconciles against, so a write that failed silently
        would leave a run looking alive forever. It is not fatal to the run itself -- the run is
        the engine's job and the ledger is its record -- so the fault is printed and the run
        continues.
        """
        if self._ledger is None:
            return
        try:
            getattr(self._ledger, action)(intent)
        except Exception as error:  # the record is an observer of the run, not the run
            print(
                f"[intents] the ledger could not record {action} for {intent.id}: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )

    def _claim(self, intent: Intent) -> bool:
        """Ask the ledger for the project's execution slot. ``True`` when this intent now owns it.

        A queue with no ledger (a test stub) always claims: the queue's own serialisation is then
        the only guarantee, which is all an in-memory queue can offer.

        A ledger that *errors* also claims, loudly. Refusing to run because the record could not be
        written would turn a transient SQLite fault into a permanently stalled engine, and the
        record has always been an observer of the run rather than the run itself.
        """
        if self._ledger is None:
            return True
        try:
            return bool(self._ledger.claim(intent))
        except Exception as error:
            print(
                f"[intents] the ledger could not claim the execution slot for {intent.id}: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return True

    def submit(self, request: IntentRequest) -> Intent:
        """Validate-and-append. Raises :class:`IntentQueueFull` when there is no room."""
        with self._condition:
            if len(self._items) >= self._capacity:
                raise IntentQueueFull(
                    f"the intent queue is full ({self._capacity}); wait for the running work"
                )
            intent = Intent(
                id=uuid.uuid4().hex,
                action_type=str(request.action_type),
                message=str(request.message),
                action_params=dict(request.action_params or {}),
                enqueued_at=time.time(),
            )
            self._items.append(intent)
            self._history.append(intent)
            self._accepted += 1
            # Written before it can run: a process that dies mid-run cannot write its own
            # epitaph, and an intent that was never written is one the next boot cannot reconcile.
            self._write("record", intent)
            self._condition.notify_all()
            return intent

    def take(self, timeout: Optional[float] = None) -> Optional[Intent]:
        """The oldest intent this project may run now, or ``None`` when the wait elapsed.

        Nothing is popped until the project's execution slot is held, so an intent that cannot
        claim stays queued at the head and this returns ``None`` -- the caller waits and comes back.
        The claim and the dequeue happen under one lock on purpose: they are one decision, and
        splitting them would let a second caller claim a slot this one is about to take.
        """
        with self._condition:
            if not self._items:
                self._condition.wait(timeout)
            if not self._items:
                return None
            intent = self._items[0]
            if not self._claim(intent):
                # The project is executing. Leave it at the head; the slot is not ours yet.
                return None
            self._items.popleft()
            intent.status = "running"
            self._current = intent
            return intent

    def release(self, intent: Intent) -> None:
        """Put a deferred intent back at the head of the queue and give the slot back.

        For the one case :meth:`take` cannot resolve by itself: the intent owns the project's
        execution slot but the run never started, because the engine was busy with a run that did
        not come from this queue.
        """
        with self._condition:
            intent.status = "queued"
            if not any(item is intent for item in self._items):
                self._items.appendleft(intent)
            if self._current is intent:
                self._current = None
            self._condition.notify_all()
        self._write("release", intent)

    def settle(self, intent: Intent, *, error: str = "") -> None:
        """Mark a drained intent finished, or failed with the reason. **Never retried.**

        A failure is terminal. Re-queueing it would be the engine spending the user's time on its
        own initiative, and the ledger would then record a run the user never asked for.
        """
        with self._condition:
            if error:
                intent.status = "failed"
                intent.error = str(error)
                self._failed += 1
            else:
                if intent.status != "stopped":
                    intent.status = "completed"
                self._completed += 1
            if self._current is intent:
                self._current = None
        self._write("settle", intent)

    def wake(self) -> None:
        """Release a waiter (used by shutdown so the worker leaves promptly)."""
        with self._condition:
            self._condition.notify_all()

    @property
    def current(self) -> Optional[Intent]:
        """The intent this queue handed out and has not settled, or ``None``.

        The authority on "what is running" for a caller **not on the run's thread**. The Stop
        endpoint is the case that matters: it runs on the API thread, and the run's context
        variable is invisible from there by design (Phase 28) -- so asking the queue is how it
        learns which intent to abort.
        """
        with self._condition:
            return self._current

    @property
    def depth(self) -> int:
        with self._condition:
            return len(self._items)

    def status(self) -> IntentQueueStatus:
        """The queue's depth and history, newest last."""
        with self._condition:
            items = [intent.view() for intent in self._history[-self._capacity:]]
            return IntentQueueStatus(
                depth=len(self._items),
                capacity=self._capacity,
                accepted=self._accepted,
                completed=self._completed,
                failed=self._failed,
                items=items,
            )


def intent_status(queue: "IntentQueue", ledger: Any = None) -> IntentQueueStatus:
    """The queue's state, plus the durable ledger that outlives it and the gate it holds.

    Factored out of the transport so the answer has exactly one implementation: the typed
    operation surface (``api.operations``) calls it through the engine service, and a test stub
    can call the same function rather than re-deriving the shape. The ledger is an observer -- a
    read that fails is reported, never allowed to take the queue's own answer with it -- because
    the queue is still the truth about what is in flight.
    """
    status = queue.status()
    if ledger is None:
        return status
    try:
        pending = ledger.pending_failure()
        return status.model_copy(
            update={
                "ledger": [IntentLedgerEntry(**row) for row in ledger.recent(20)],
                "pending_failure": IntentLedgerEntry(**pending) if pending is not None else None,
            }
        )
    except Exception as error:  # the queue still answers; the ledger is an observer
        print(
            f"[intents] the ledger could not be read: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return status


def submit_intent(
    queue: "IntentQueue",
    action_type: str = "custom",
    message: str = "",
    action_params: Any = None,
) -> Dict[str, Any]:
    """Validate-and-append one intent, answered in the wire shape. Never raises.

    The same reasoning as :func:`intent_status`: the *service* is what ``api.operations`` calls, but
    the queue mechanics -- strict request model, the bounded refusal -- have exactly one home so a
    test stub and the production service cannot disagree about what a submitted intent answers.

    A full queue is a :class:`ServiceRefusal` (``success: false``) rather than a bare ``error``,
    because the gateway refuses to forward an error that does not say it is one.
    """
    try:
        intent = queue.submit(
            IntentRequest(
                action_type=action_type,
                message=message,
                action_params=dict(action_params or {}),
            )
        )
    except IntentQueueFull as error:
        return {"success": False, "error": str(error)}
    return {
        "success": True,
        "intent_id": intent.id,
        "status": "queued",
        "position": queue.depth,
    }


def run_worker(
    queue: IntentQueue,
    handler: Callable[[Intent], None],
    stop_event: threading.Event,
    *,
    poll_seconds: float = 0.25,
) -> None:
    """Drain ``queue`` into ``handler`` until ``stop_event`` is set.

    This is the background orchestrator loop. ``handler`` owns what "running an intent" means --
    in the app it launches the workflow and waits for it, which is what keeps the queue serial
    without the queue having to know about threads or run locks.

    A handler that raises fails *its* intent and no other: the loop is the thing that must not
    die, because a dead loop turns every later submission into a receipt for work that never
    runs.

    ``RunDeferred`` is the exception to that rule, and the only one: it says the run was never
    started, so the intent is handed back to the queue instead of being failed. A busy engine is a
    reason to wait, not an outcome to record.
    """
    while not stop_event.is_set():
        intent = queue.take(timeout=poll_seconds)
        if intent is None:
            continue
        try:
            handler(intent)
        except RunDeferred:
            queue.release(intent)
            continue
        except Exception as error:  # the loop outlives any one intent
            queue.settle(intent, error=f"{type(error).__name__}: {error}")
            continue
        queue.settle(intent)
