"""The intent queue, and the loop that drains it.

The frontend cannot spawn anything. What it can do is *ask*, and this is where the asking stops
being the frontend's problem: a request that validates is appended here and answered with a
receipt, and the orchestrator takes it from the queue when it is free.

Two properties are the point:

* **Bounded.** A queue with no ceiling is a memory leak with a nice name; a client that submits
  faster than runs complete is refused rather than accumulated.
* **Serialised.** One intent is drained at a time. The engine runs one workflow at a time
  (``BridgeAPI`` holds the run lock), and a queue that fanned out would turn "two clicks" into
  two concurrent writers over the same plan.

The worker is a plain function over the queue, so it can be driven by a thread in the app and by
a test with no thread at all.
"""

from __future__ import annotations

import collections
import dataclasses
import threading
import time
import uuid
from typing import Callable, Deque, Dict, List, Optional

from api.schemas import IntentQueueStatus, IntentRequest, IntentView

# A UI that has queued this many unrun intents is not asking for work, it is stuck; refusing the
# next one is the honest answer, and the frontend gets a 429 rather than an ever-growing list.
DEFAULT_CAPACITY = 32


class IntentQueueFull(Exception):
    """The queue is at capacity. A refusal, not a silent drop."""


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
            enqueued_at=self.enqueued_at,
        )


class IntentQueue:
    """A bounded FIFO of validated intents, safe to submit to and drain from any thread."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self._capacity = max(1, int(capacity))
        self._items: Deque[Intent] = collections.deque()
        self._history: List[Intent] = []
        self._condition = threading.Condition()
        self._accepted = 0
        self._completed = 0
        self._failed = 0

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
            self._condition.notify_all()
            return intent

    def take(self, timeout: Optional[float] = None) -> Optional[Intent]:
        """The oldest queued intent, or ``None`` when the wait elapsed."""
        with self._condition:
            if not self._items:
                self._condition.wait(timeout)
            if not self._items:
                return None
            intent = self._items.popleft()
            intent.status = "running"
            return intent

    def settle(self, intent: Intent, *, error: str = "") -> None:
        """Mark a drained intent finished, or failed with the reason."""
        with self._condition:
            if error:
                intent.status = "failed"
                intent.error = str(error)
                self._failed += 1
            else:
                intent.status = "completed"
                self._completed += 1

    def wake(self) -> None:
        """Release a waiter (used by shutdown so the worker leaves promptly)."""
        with self._condition:
            self._condition.notify_all()

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
    """
    while not stop_event.is_set():
        intent = queue.take(timeout=poll_seconds)
        if intent is None:
            continue
        try:
            handler(intent)
        except Exception as error:  # the loop outlives any one intent
            queue.settle(intent, error=f"{type(error).__name__}: {error}")
            continue
        queue.settle(intent)
