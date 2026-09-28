"""The daily API-call accountant (`/var/lib/sa02m-homeconnect/budget.json`).

BSH allows 1000 calls per day per client+user and counts failed calls too
(docs/decisions/homekit-home-connect.md G7). Every call to the cloud — REST,
event-stream connect, token request — is charged BEFORE it is sent, so a call
that fails or a crash mid-request is still counted, and the count is
persisted at every charge, so a restart does not reset it.

Policy (`check`):
* a server `Retry-After` (HTTP 429) blocks EVERY call until it passes;
* at DAILY_LIMIT nothing is sent until the next UTC day;
* at LOCAL_BUDGET (800) only the event stream and token requests continue —
  the 200-call margin keeps the stream and the sign-in alive for the rest of
  the day.

The day boundary is UTC midnight. Whether BSH resets its window at UTC
midnight or on a rolling 24 h window is unverified (G7); the local margin is
the hedge. A budget file that cannot be trusted (unreadable, foreign owner,
malformed) counts as LOCAL_BUDGET already used — fail toward fewer calls.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import constants as C
from .fsutil import UnsafeFile, atomic_write, read_private

log = logging.getLogger("sa02m_homeconnect.budget")


class BudgetExceeded(Exception):
    def __init__(self, reason: str, until: int) -> None:
        self.reason = reason
        self.until = int(until)
        super().__init__("%s until %d" % (reason, self.until))


def utc_day(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")


def next_utc_midnight(ts: float) -> int:
    day = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).date() + _dt.timedelta(days=1)
    return int(_dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc).timestamp())


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("bad count")
    return value


class Budget:
    def __init__(self, path: str = C.BUDGET_FILE, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self.day = utc_day(clock())
        self.used = 0
        self.by_kind: Dict[str, int] = {k: 0 for k in C.KINDS}
        self.blocked_until = 0
        self.blocked_reason = ""
        self.untrusted = False
        self.persist_failed = False
        self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            raw, _st = read_private(self.path, 4096)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            day = data.get("day")
            if not isinstance(day, str) or len(day) != 10:
                raise ValueError("bad day")
            used = _count(data.get("used"))
            by_kind = {k: 0 for k in C.KINDS}
            raw_kinds = data.get("by_kind") or {}
            if not isinstance(raw_kinds, dict):
                raise ValueError("bad by_kind")
            for kind in C.KINDS:
                by_kind[kind] = _count(raw_kinds.get(kind, 0))
            blocked_until = _count(data.get("blocked_until", 0))
            reason = data.get("blocked_reason", "")
            self.day, self.used, self.by_kind = day, used, by_kind
            self.blocked_until = blocked_until
            self.blocked_reason = reason if reason in C.REASONS else ""
        except FileNotFoundError:
            return
        except (UnsafeFile, OSError, ValueError, UnicodeDecodeError) as exc:
            log.error("budget file %s not trusted (%s) — counting %d calls as used today",
                      self.path, exc, C.LOCAL_BUDGET)
            self.untrusted = True
            self.day = utc_day(self._clock())
            self.used = C.LOCAL_BUDGET
            self.by_kind = {k: 0 for k in C.KINDS}
        self._roll(self._clock())

    def _persist(self) -> None:
        body = {
            "day": self.day,
            "used": self.used,
            "by_kind": dict(self.by_kind),
            "blocked_until": self.blocked_until,
            "blocked_reason": self.blocked_reason,
        }
        try:
            atomic_write(self.path, json.dumps(body, sort_keys=True) + "\n", mode=0o600)
            self.persist_failed = False
        except OSError as exc:
            # The in-memory count still bounds this process; say so loudly.
            if not self.persist_failed:
                log.error("budget file %s not writable: %s — counting in memory", self.path, exc)
            self.persist_failed = True

    def _roll(self, now: float) -> None:
        today = utc_day(now)
        if today != self.day:
            self.day = today
            self.used = 0
            self.by_kind = {k: 0 for k in C.KINDS}
            self.untrusted = False
        if self.blocked_until and self.blocked_until <= now:
            self.blocked_until = 0
            self.blocked_reason = ""

    # ── policy ───────────────────────────────────────────────────────────
    def _refusal(self, kind: str, now: float) -> Optional[BudgetExceeded]:
        if self.blocked_until > now:
            return BudgetExceeded(self.blocked_reason or C.REASON_RETRY_AFTER, self.blocked_until)
        if self.used >= C.DAILY_LIMIT:
            return BudgetExceeded(C.REASON_DAILY_LIMIT, next_utc_midnight(now))
        if self.used >= C.LOCAL_BUDGET and kind == C.KIND_API:
            return BudgetExceeded(C.REASON_BUDGET_LOCAL, next_utc_midnight(now))
        return None

    def check(self, kind: str) -> None:
        """Raise BudgetExceeded when a call of `kind` may not be sent now."""
        if kind not in C.KINDS:
            raise ValueError("unknown call kind %r" % kind)
        with self._lock:
            now = self._clock()
            self._roll(now)
            refusal = self._refusal(kind, now)
        if refusal is not None:
            raise refusal

    def charge(self, kind: str) -> int:
        """Count one call of `kind` (after `check`); returns today's total."""
        if kind not in C.KINDS:
            raise ValueError("unknown call kind %r" % kind)
        with self._lock:
            now = self._clock()
            self._roll(now)
            refusal = self._refusal(kind, now)
            if refusal is not None:
                raise refusal
            self.used += 1
            self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
            self._persist()
            return self.used

    def block_for(self, seconds: int, reason: str = C.REASON_RETRY_AFTER) -> int:
        """A server Retry-After: no call of any kind until it passes."""
        with self._lock:
            now = self._clock()
            until = int(now + max(1, min(int(seconds), C.RETRY_AFTER_MAX_S)))
            if until > self.blocked_until:
                self.blocked_until = until
                self.blocked_reason = reason
            self._persist()
            return self.blocked_until

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            self._roll(now)
            return {
                "day": self.day,
                "used": self.used,
                "limit": C.DAILY_LIMIT,
                "local": C.LOCAL_BUDGET,
                "remaining": max(0, C.DAILY_LIMIT - self.used),
                "by_kind": dict(self.by_kind),
                "blocked_until": self.blocked_until,
                "blocked_reason": self.blocked_reason,
                "untrusted": self.untrusted,
                "persist_failed": self.persist_failed,
            }
