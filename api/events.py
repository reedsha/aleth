"""The subscriber hub the SSE endpoint streams from.

The frontend must not poll SQLite: a poll is a query per client per interval, and it is *late*
by construction -- it shows the state that was true when it asked. The engine already announces
every transition on one typed bus (``bridge_bus``); this hub is a second consumer of that same
stream, so the push the desktop window gets and the push an SSE client gets are the *same*
validated event, not two renderings that can drift.

Two rules make it safe to put on the emitter's path:

* **Publishing never blocks.** Each subscriber has a bounded queue; a client that cannot keep up
  loses its oldest frames rather than stalling the workflow thread that emitted them. The count
  of dropped frames is kept, because a silently truncated stream is a lie.
* **Publishing never raises.** A subscriber that has gone away must not be able to break the
  emit that reached it.
"""

from __future__ import annotations

import collections
import json
import threading
from typing import Any, Deque, Dict, List, Optional

# Per-subscriber backlog. At 256 frames the buffer is far more than a UI needs to survive a
# render, and bounded enough that a wedged client cannot grow the process.
DEFAULT_BACKLOG = 256

# The SSE field framing. The JSON is serialised compactly and ASCII-escaped, so it can never
# contain a newline that would break the frame -- the same reasoning as ``bridge_bus``'s sink
# call, applied to a text protocol instead of a JS one.
FRAME_PREFIX = "data: "
FRAME_SUFFIX = "\n\n"
# A comment line: valid SSE, ignored by clients, and what keeps a quiet stream from looking dead.
HEARTBEAT_FRAME = ": keepalive\n\n"


def sse_frame(event: Dict[str, Any]) -> str:
    """One ``data:`` frame carrying ``event`` as compact JSON."""
    payload = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
    return f"{FRAME_PREFIX}{payload}{FRAME_SUFFIX}"


class Subscriber:
    """One client's bounded backlog, with a blocking read for the streaming thread."""

    def __init__(self, backlog: int = DEFAULT_BACKLOG):
        self._backlog = max(1, int(backlog))
        self._frames: Deque[str] = collections.deque()
        self._condition = threading.Condition()
        self._closed = False
        self._dropped = 0

    def push(self, frame: str) -> None:
        """Append a frame, dropping the oldest when full. Never blocks, never raises."""
        with self._condition:
            if self._closed:
                return
            while len(self._frames) >= self._backlog:
                self._frames.popleft()
                self._dropped += 1
            self._frames.append(frame)
            self._condition.notify_all()

    def next_frame(self, timeout: Optional[float] = None) -> Optional[str]:
        """The next frame, or ``None`` when the wait elapsed (the caller then heartbeats)."""
        with self._condition:
            if not self._frames:
                self._condition.wait(timeout)
            if self._frames:
                return self._frames.popleft()
            return None

    def close(self) -> None:
        """Mark the subscriber gone, releasing any waiter."""
        with self._condition:
            self._closed = True
            self._frames.clear()
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dropped(self) -> int:
        return self._dropped


class EventHub:
    """Every live SSE subscriber, and the one entry point the bus calls."""

    def __init__(self, backlog: int = DEFAULT_BACKLOG):
        self._backlog = int(backlog)
        self._subscribers: List[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(self._backlog)
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        subscriber.close()
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def publish(self, event: Dict[str, Any]) -> None:
        """Fan one validated event out to every subscriber. Never blocks, never raises."""
        try:
            text = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError):
            # An unserialisable event is the bus's problem, not the stream's; the bus has
            # already validated it, so this is unreachable in practice and harmless if not.
            return
        self.publish_text(text)

    def publish_text(self, json_text: str) -> None:
        """Fan out an event the bus already serialised. This is the transport's entry point."""
        frame = f"{FRAME_PREFIX}{json_text}{FRAME_SUFFIX}"
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.push(frame)

    def close_all(self) -> None:
        """Release every subscriber (server shutdown)."""
        with self._lock:
            subscribers, self._subscribers = list(self._subscribers), []
        for subscriber in subscribers:
            subscriber.close()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class HubTransport:
    """The bus's one transport: publish each validated event to the stream's subscribers.

    This replaced the pywebview push. ``bridge_bus`` used to inject a call into the window with
    ``evaluate_js``; now the bus's transport *is* the stream, and the window is a browser that
    reads it like any other client. That is the whole of the cutover: one delivery path, and the
    desktop window is no longer special.
    """

    def __init__(self, hub: EventHub):
        self._hub = hub

    def send(self, json_text: str) -> None:
        self._hub.publish_text(json_text)
