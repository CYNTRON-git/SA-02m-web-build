#!/usr/bin/env python3
"""Логгер телеметрии ДТВ / СЭ-02м-3 / MR-02m / Carel → SQLite (USB → SD → eMMC).

  python3 scripts/sa02m_devices_logger.py
  STAND_DEVICES_LOG_INTERVAL_S=1 STAND_DEVICES_PURGE_EVERY_S=3600 ...

The per-tick body is `logger_tick()` — the one place every archive write is
wired, and the one tests/test_logger_tick.py executes: an insert that falls
out of it is a RED test, not a silent gap (audit 2026-09-08, E5).

Ticks are buffered and written as one batch every FLUSH_EVERY_S on one
long-lived connection (sa02m_devices/history_writer.py): a connection opened
and closed per write was a full checkpoint each time — the eMMC wear of audit
2026-11-11 IO-01. A clean stop (SIGTERM/SIGINT) writes the buffer before exit.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sa02m_devices import history_store, event_journal_state as journal  # noqa: E402
from sa02m_devices.device_history_db import rotate_if_needed  # noqa: E402
from sa02m_devices.device_history_migrate import (  # noqa: E402
    _db_has_rows,
    promote_on_backend_change,
)
from sa02m_devices.devices_widgets import filter_for_archive  # noqa: E402
from sa02m_devices.history_writer import ArchiveWriter  # noqa: E402
from sa02m_devices.stand_devices import live_snapshot  # noqa: E402
from sa02m_devices.stand_storage_path import (  # noqa: E402
    emmc_staging_path,
    invalidate_resolve_cache,
    resolve_storage_target,
)

INTERVAL_S = float(os.environ.get("STAND_DEVICES_LOG_INTERVAL_S", "1"))
INTERVAL_EMMC_S = float(os.environ.get("STAND_DEVICES_LOG_INTERVAL_EMMC_S", "5"))
# MR-02m AI moves slowly (temp/pressure/current) — its own cadence, independent
# of the dtv/ce tick (do NOT slow dtv/ce). 10 s ≈ 10× lighter than 1 Hz.
MR_INTERVAL_S = float(os.environ.get("STAND_DEVICES_MR_INTERVAL_S", "10"))
PURGE_EVERY_S = float(os.environ.get("STAND_DEVICES_PURGE_EVERY_S", "3600"))
RESOLVE_EVERY_S = float(os.environ.get("STAND_DEVICES_RESOLVE_EVERY_S", "10"))
# The one home of the batch interval (owner's decision 2026-11-11: 30 s). The
# archive tail lags by up to this plus one tick; a crash loses at most that.
FLUSH_EVERY_S = float(os.environ.get("STAND_DEVICES_FLUSH_EVERY_S", "30"))

_stop = False


def _on_term(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True


@dataclass
class LoggerState:
    """Cadence markers the tick body carries between calls (monotonic / wall).

    `flush_every_s` 0 writes every tick (one transaction per tick); main()
    passes FLUSH_EVERY_S. `writer` is created on the first tick."""

    last_mr: float = 0.0     # monotonic — MR + Carel sample cadence
    last_purge: float = 0.0  # wall clock — purge_old cadence
    flush_every_s: float = 0.0
    writer: ArchiveWriter | None = None


def _tick_interval(target) -> float:
    interval = INTERVAL_EMMC_S if target.backend == "emmc" else INTERVAL_S
    return max(0.2, interval)


def _writer_for(target, state: LoggerState) -> ArchiveWriter:
    interval = _tick_interval(target)
    writer = state.writer
    if writer is None:
        writer = ArchiveWriter(
            target.active_path,
            flush_every_s=state.flush_every_s,
            interval_s=interval,
        )
        state.writer = writer
    else:
        writer.retarget(target.active_path)
        writer.flush_every_s = state.flush_every_s
        writer.interval_s = interval
    return writer


def close_writer(state: LoggerState) -> None:
    """Write the buffer and release the archive file (before a promote, a
    staging absorb, or exit)."""
    if state.writer is not None:
        state.writer.close()


def logger_tick(target, state: LoggerState, *, now_m: float | None = None) -> None:
    """One archive tick against `target.active_path`.

    snapshot → filter removed widgets → buffer. Once per batch the writer
    replays the buffer in tick order in one transaction: dtv/ce rows (every
    tick) → MR/MTD rows (MR_INTERVAL_S) → CE spike events + Carel
    alarm/state edges (every tick, cooldown 0, at the tick's own ts) → Carel
    rows (MR_INTERVAL_S). After a written batch: rotate → purge.
    Storage-target resolution and the eMMC promote stay in main(): they change
    `target`, this body only writes to it.
    """
    now_m = time.monotonic() if now_m is None else now_m
    with journal.observation(target.active_path) as captured:
        snap = live_snapshot()
    try:
        snap = filter_for_archive(snap)
    except Exception:  # noqa: BLE001
        pass
    writer = _writer_for(target, state)
    # MR-02m AI and Carel on their own (slower) cadence — same already-built
    # snap, no extra bus/MQTT I/O; only the SQLite write is added.
    mr_due = now_m - state.last_mr >= MR_INTERVAL_S
    writer.add(snap, mr_due=mr_due, captured=captured)
    if mr_due:
        state.last_mr = now_m
    if state.flush_every_s > 0 and not writer.due(now_m):
        return
    created = writer.flush(now_m=now_m)
    for ev in created[:5]:
        print(f"event: {ev.get('message')}", flush=True)
    # A stat, not a close: the writer releases the file only when a rotation
    # is really due, so the steady state keeps its one connection.
    if writer.size_at_least(history_store.ROTATE_BYTES):
        writer.close()
        rot = rotate_if_needed(path=target.active_path)
        if rot.get("rotated"):
            print(f"rotate: {rot}", flush=True)
        elif rot.get("skipped"):
            print(f"rotate skipped: {rot}", flush=True)

    now = time.time()
    if now - state.last_purge >= PURGE_EVERY_S:
        stats = writer.purge()
        state.last_purge = now
        if (
            stats["dtv_deleted"]
            or stats["ce_deleted"]
            or stats.get("mr_deleted")
            or stats.get("carel_deleted")
            or stats.get("mtd_deleted")
            or stats.get("events_deleted")
        ):
            print(
                f"purge: dtv={stats['dtv_deleted']} ce={stats['ce_deleted']} "
                f"mr={stats.get('mr_deleted', 0)} "
                f"carel={stats.get('carel_deleted', 0)} "
                f"mtd={stats.get('mtd_deleted', 0)} "
                f"events={stats.get('events_deleted', 0)}",
                flush=True,
            )


def _sleep_until(end: float, stopped) -> None:
    """Sleep in <=0.2 s slices until monotonic `end` or until `stopped()`."""
    while not stopped():
        # One clock read per slice: a second read could land past `end` and
        # hand sleep() a negative length (ValueError — the 1.135 crash loop).
        rem = end - time.monotonic()
        if rem <= 0:
            break
        time.sleep(min(0.2, rem))


def _staging_absorb_due(target) -> bool:
    """True when the staging absorb below will really merge into the archive
    the writer holds — the same test promote_on_backend_change makes. A bare
    is_file() would close the writer on EVERY tick while an empty staging
    file lingers, which is the per-tick checkpoint this writer removes."""
    staging = emmc_staging_path()
    try:
        if not staging.is_file() or staging.resolve() == target.active_path.resolve():
            return False
    except OSError:
        return False
    return _db_has_rows(staging)


def _try_promote(state: LoggerState, target, source=(None, None)):
    """A failed history transfer must not prevent sampling the selected media."""
    try:
        if source[0] is not None or _staging_absorb_due(target):
            close_writer(state)
        report = promote_on_backend_change(
            source[0], source[1], target.backend, target.active_path
        )
    except Exception as exc:  # noqa: BLE001
        report = {"ok": False, "error": str(exc)}
    if report:
        if not report.get("ok"):
            print(f"logger error: archive promotion failed: {report}", file=sys.stderr, flush=True)
        elif any(s.get("action") in ("copy", "merge") for s in report.get("steps", [])):
            print(f"promote: {report}", flush=True)
    return report


def main() -> int:
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    state = LoggerState(flush_every_s=FLUSH_EVERY_S)
    target = resolve_storage_target(force_refresh=True)
    print(
        f"devices logger: backend={target.backend} path={target.active_path} "
        f"interval_usb_sd={INTERVAL_S}s interval_emmc={INTERVAL_EMMC_S}s "
        f"interval_mr={MR_INTERVAL_S}s purge_every={PURGE_EVERY_S}s "
        f"flush_every={FLUSH_EVERY_S}s",
        flush=True,
    )
    try:
        _run_loop(state, target)
    finally:
        # SIGTERM/SIGINT end the loop; an exception escaping it lands here too.
        close_writer(state)
    print("devices logger stopped", flush=True)
    return 0


def _run_loop(state: LoggerState, target) -> None:
    last_resolve = 0.0
    # Only failed transfers are retained, independently of the recording
    # target. Sources are the canonical targets actually visited this run.
    pending_sources: dict[tuple[str, Path], None] = {}
    while not _stop:
        t0 = time.monotonic()
        interval = _tick_interval(target)
        promotion_attempted = False
        try:
            now_m = time.monotonic()
            if now_m - last_resolve >= RESOLVE_EVERY_S:
                invalidate_resolve_cache()
                new_target = resolve_storage_target(force_refresh=True)
                sources = list(pending_sources)
                if (
                    new_target.backend != target.backend
                    or new_target.active_path != target.active_path
                ):
                    # The buffer belongs to the OLD file: write it there, and
                    # release the file, before the promote moves it.
                    close_writer(state)
                    source = (target.backend, target.active_path)
                    if source not in sources:
                        sources.append(source)
                    print(
                        f"devices logger: switch {target.backend} → "
                        f"{new_target.backend} path={new_target.active_path}",
                        flush=True,
                    )
                target = new_target
                for source in sources:
                    if source[1] == target.active_path:
                        pending_sources.pop(source, None)
                        continue
                    if source in pending_sources and not source[1].is_file():
                        continue  # removed media may return on a later resolve
                    promotion_attempted = True
                    report = _try_promote(state, target, source)
                    steps = report.get("steps", []) if report else []
                    completed = {s.get("source") for s in steps if s.get("ok")}
                    for done in list(pending_sources):
                        if str(done[1]) in completed:
                            pending_sources.pop(done, None)
                    if report and not report.get("ok") and str(source[1]) not in completed:
                        pending_sources[source] = None
                    elif source in pending_sources:
                        if source[1].is_file() and not _db_has_rows(source[1]):
                            pending_sources.pop(source, None)
                    staging = emmc_staging_path()
                    if any(not s.get("ok") and s.get("source") == str(staging) for s in steps):
                        pending_sources[("emmc", staging)] = None
                last_resolve = now_m

            # Absorb eMMC staging whenever on removable (even without switch)
            # A failed absorb joins the same resolve-cadence retry queue.
            if target.backend in ("usb", "sd") and not promotion_attempted and not pending_sources:
                report = _try_promote(state, target)
                if report and not report.get("ok"):
                    pending_sources[("emmc", emmc_staging_path())] = None

            logger_tick(target, state, now_m=now_m)
        except Exception as exc:  # noqa: BLE001
            print(f"logger error: {exc}", file=sys.stderr, flush=True)
            invalidate_resolve_cache()
            try:
                target = resolve_storage_target(force_refresh=True)
            except Exception:  # noqa: BLE001
                pass

        elapsed = time.monotonic() - t0
        sleep_s = interval - elapsed
        if sleep_s > 0:
            _sleep_until(time.monotonic() + sleep_s, lambda: _stop)


if __name__ == "__main__":
    raise SystemExit(main())
