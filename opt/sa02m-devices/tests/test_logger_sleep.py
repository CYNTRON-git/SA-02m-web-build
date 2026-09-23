"""F1: the logger's inter-tick wait never passes a negative length to sleep.

The wait loop read the clock twice — once in the `< end` check and again inside
`min(0.2, end - now)` — so a tick that crossed `end` between the two reads asked
`time.sleep()` for a negative length: `ValueError: sleep length must be
non-negative`, uncaught, and systemd restarted the daemon (restart counter 78 on
bench 1.135, 2026-09-23). The clock is scripted here to cross `end` exactly
between the two reads; the real `time.sleep` stays in place, because its
ValueError IS the defect.
"""

from __future__ import annotations

import itertools

import sa02m_devices_logger as logger

END = 1000.0


def _clock(monkeypatch, *reads: float) -> list[float]:
    """Script time.monotonic(): the given reads, then END + 1 forever."""
    seen: list[float] = []
    values = itertools.chain(reads, itertools.repeat(END + 1.0))

    def fake() -> float:
        v = next(values)
        seen.append(v)
        return v

    monkeypatch.setattr(logger.time, "monotonic", fake)
    return seen


def test_clock_crossing_end_between_reads_does_not_crash(monkeypatch):
    # read 1 (loop check): still before END; read 2: already past END.
    _clock(monkeypatch, END - 0.001, END + 0.001)
    logger._sleep_until(END, lambda: False)  # RED: ValueError before the fix


def test_stop_flag_ends_the_wait_without_reading_the_clock_twice(monkeypatch):
    seen = _clock(monkeypatch, END - 5.0)
    logger._sleep_until(END, lambda: True)
    assert len(seen) <= 1


def test_wait_sleeps_in_slices_no_longer_than_200ms(monkeypatch):
    _clock(monkeypatch, END - 0.5, END - 0.5, END - 0.1, END - 0.1)
    slept: list[float] = []
    monkeypatch.setattr(logger.time, "sleep", slept.append)
    logger._sleep_until(END, lambda: False)
    assert slept and all(0 < s <= 0.2 for s in slept), slept
