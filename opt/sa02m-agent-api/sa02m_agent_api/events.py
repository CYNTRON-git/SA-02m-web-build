# -*- coding: utf-8 -*-
"""In-process fan-out for SSE. A full subscriber queue drops the event."""

import queue
import threading


class Bus:
    def __init__(self, max_subs=8):
        self.max_subs = max_subs
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self, owner, job_id=None):
        q = queue.Queue(maxsize=100)
        with self._lock:
            if len(self._subs) >= self.max_subs:
                return None
            self._subs.append((q, owner, job_id))
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs = [s for s in self._subs if s[0] is not q]

    def publish(self, event, *, owner=None, job_id=None):
        # Routing comes from the publisher, never from fields in the payload.
        private = owner is not None or job_id is not None
        if private and (not owner or not job_id):
            return
        with self._lock:
            subs = list(self._subs)
        for q, sub_owner, sub_job in subs:
            if private:
                if sub_owner != owner or (sub_job is not None and sub_job != job_id):
                    continue
            elif sub_job is not None:
                continue
            try:
                q.put_nowait(event)
            except queue.Full:
                continue
