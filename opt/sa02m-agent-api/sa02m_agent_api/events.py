# -*- coding: utf-8 -*-
"""In-process fan-out for SSE. A full subscriber queue drops the event."""

import queue
import threading


class Bus:
    def __init__(self, max_subs=8):
        self.max_subs = max_subs
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self._lock:
            if len(self._subs) >= self.max_subs:
                return None
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs = [s for s in self._subs if s is not q]

    def publish(self, event):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                continue
