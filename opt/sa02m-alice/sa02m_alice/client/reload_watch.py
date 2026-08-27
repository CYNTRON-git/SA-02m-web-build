"""In-place device-document reload: watch, retained grace, subscription diff.

A binding edit used to apply by restarting the whole unit, which cost the
account a 60–150 s window with ZERO devices (the Socket.IO session went down
with the process). The registry could already `reload()` in place; what was
missing is a trigger, a subscription diff, and retained handling for the
topics a reload newly subscribes.

The trigger is the device document's own mtime/inode, polled by the watchdog
loop that already re-reads client_enabled() every second. It needs no new
verb, no signal semantics, no unit change and no sudoers change — and it
covers EVERY writer (the CGI, a hand edit, an offline restore), not just one
caller. Rationale and the rejected alternatives: the 1.0.6.19 plan; the seam
this module keeps is homed in docs/contracts/alice-mqtt-mapping.md.

Nothing here imports main.py, so every unit is independently testable.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple

Fingerprint = Tuple[int, int, int]


def devices_fingerprint(path: str) -> Optional[Fingerprint]:
    """(inode, mtime_ns, size) of the device document; None when unreadable.

    None is a legitimate value, not an error: `load_devices` returns an empty
    document for an absent file, so absent→present must count as a change.
    Never raises — this runs on the watchdog tick.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (int(st.st_ino), int(st.st_mtime_ns), int(st.st_size))


class DevicesWatcher:
    """Polls the device document for a change, one stat per call."""

    def __init__(self, path: str) -> None:
        self._path = path
        # Seed at construction so a quiet first tick does not report a change.
        self._fp = devices_fingerprint(path)

    @property
    def path(self) -> str:
        return self._path

    def changed(self) -> bool:
        """True when the document changed since the last call.

        The new fingerprint is COMMITTED BEFORE the caller loads the document.
        That ordering is load-bearing: a write landing during the load is then
        seen again on the next tick — one redundant reload, never a missed one.
        Load-then-commit would record the new fingerprint while holding the old
        document and miss that write permanently.
        """
        fp = devices_fingerprint(self._path)
        previous, self._fp = self._fp, fp
        return fp != previous

    def arm(self) -> None:
        """Re-baseline without acting — used on the connect path, which
        subscribes from the document it just fingerprinted."""
        self.changed()


class RetainedGrace:
    """Per-topic window in which a retained message is cached, never reported.

    The connect path has a single global window (~1 s after subscribe). A
    reload subscribes new topics in steady state, where that flag is off, so
    without this the retained burst of every added topic would be reported as
    a change — a burst of events that never happened, and a violation of the
    1.0.6.16 rule (cache retained, never report it).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._until: Dict[str, float] = {}

    def arm(self, topics: Iterable[str], window_s: float) -> None:
        deadline = self._clock() + float(window_s)
        with self._lock:
            for topic in topics:
                # Re-arming just extends the window — a redundant reload is safe.
                self._until[topic] = deadline

    def suppress(self, topic: str) -> bool:
        """True while `topic` is inside its grace window. Prunes on access, so
        the map cannot grow past the topics currently armed."""
        now = self._clock()
        with self._lock:
            if not self._until:
                return False
            expired = [t for t, until in self._until.items() if until <= now]
            for t in expired:
                del self._until[t]
            return topic in self._until

    def armed_count(self) -> int:
        with self._lock:
            return len(self._until)


def apply_reload(
    registry: Any,
    mqtt: Any,
    grace: RetainedGrace,
    *,
    window_s: float,
    log: logging.Logger,
) -> Tuple[Set[str], Set[str]]:
    """Re-read the device document and diff the MQTT subscriptions.

    Returns (added, removed) topic sets — empty when nothing changed or when
    the document could not be loaded. `mqtt` is duck-typed (subscribe /
    unsubscribe) so tests can pass a fake.
    """
    old: Set[str] = set(registry.mqtt_topics())
    try:
        registry.reload()
    except Exception as exc:
        # DeviceRegistry.reload() assigns the freshly loaded document FIRST, so
        # a corrupt (hand-edited) file leaves the previous document and every
        # index untouched. Keep serving it — but never silently: a reload that
        # stopped working must be visible in the journal.
        log.error("device document reload failed, keeping the previous set: %s", exc)
        return set(), set()

    new: Set[str] = set(registry.mqtt_topics())
    added = new - old
    removed = old - new

    for topic in sorted(removed):  # sorted() for deterministic assertions only
        try:
            mqtt.unsubscribe(topic)
        except Exception as exc:
            log.error("unsubscribe failed for %s: %s", topic, exc)

    if added:
        # ARM BEFORE SUBSCRIBE. The broker can deliver the retained burst on
        # the paho thread before subscribe() even returns here; arming after
        # would let those messages be reported as state changes.
        grace.arm(added, window_s)
        for topic in sorted(added):
            try:
                mqtt.subscribe(topic, qos=1)
            except Exception as exc:
                log.error("subscribe failed for %s: %s", topic, exc)

    if added or removed:
        log.info(
            "device document reloaded: +%d/-%d topics", len(added), len(removed)
        )
    return added, removed
