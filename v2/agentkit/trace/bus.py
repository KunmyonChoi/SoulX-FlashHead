"""In-process publish/subscribe for live updates (the SSE endpoint listens here).

publish() is called from the recorder's writer thread and from whoever stores a document.
It never blocks: a subscriber is a plain callable that must return at once (the SSE endpoint
hands the event to its event loop with call_soon_threadsafe). A subscriber that raises is removed.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("agentkit.trace")

Subscriber = Callable[[dict], None]


class Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[int, Subscriber] = {}
        self._next = 0

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """Returns the function that unsubscribes."""
        with self._lock:
            self._next += 1
            token = self._next
            self._subs[token] = fn

        def unsubscribe() -> None:
            with self._lock:
                self._subs.pop(token, None)

        return unsubscribe

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs.items())
        for token, fn in subs:
            try:
                fn(event)
            except Exception as exc:          # closed event loop, broken client, ...
                log.debug("trace bus: dropping subscriber (%s)", exc)
                with self._lock:
                    self._subs.pop(token, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
