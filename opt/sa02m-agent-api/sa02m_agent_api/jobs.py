# -*- coding: utf-8 -*-
"""Long operations return a job id. The work runs on a daemon thread."""

import secrets
import threading
import time


class Jobs:
    MAX_KEPT = 200  # finished records beyond this are dropped, oldest first

    def __init__(self, bus=None):
        self.bus = bus
        self._lock = threading.Lock()
        self._jobs = {}

    def start(self, owner, fn):
        jid = secrets.token_hex(8)
        rec = {"id": jid, "owner": owner, "status": "running", "result": None, "ts": int(time.time())}
        with self._lock:
            self._jobs[jid] = rec
            if len(self._jobs) > self.MAX_KEPT:
                done = sorted((r for r in self._jobs.values() if r["status"] != "running"),
                              key=lambda r: r["ts"])
                for old in done[: len(self._jobs) - self.MAX_KEPT]:
                    self._jobs.pop(old["id"], None)
            self._emit(owner, jid, {"event": "start", "job_id": jid})

        def run():
            try:
                result = fn()
                status = "done"
            except Exception as exc:  # noqa: BLE001 — the job must finish
                result = {"ok": False, "error": type(exc).__name__}
                status = "error"
            with self._lock:
                rec["status"] = status
                rec["result"] = result
                self._emit(owner, jid, {"event": status, "job_id": jid, "result": result})

        threading.Thread(target=run, name="agent-job-" + jid, daemon=True).start()
        return jid

    def get(self, jid, owner):
        with self._lock:
            rec = self._jobs.get(jid)
            if not rec or rec.get("owner") != owner:
                return None
            return {"id": rec["id"], "status": rec["status"], "result": rec["result"]}

    def subscribe(self, jid, owner):
        """Return (found, queue); a missing queue means no subscription capacity."""
        # Jobs -> Bus is the lock order for both subscription and publication,
        # so the initial state cannot miss or be overtaken by a transition.
        with self._lock:
            rec = self._jobs.get(jid)
            if not rec or rec.get("owner") != owner:
                return False, None
            q = self.bus.subscribe(owner, jid) if self.bus is not None else None
            if q is not None:
                q.put_nowait({"event": "state", "job_id": jid, "status": rec["status"],
                              "result": rec["result"]})
            return True, q

    def _emit(self, owner, jid, event):
        if self.bus is not None:
            self.bus.publish(event, owner=owner, job_id=jid)
