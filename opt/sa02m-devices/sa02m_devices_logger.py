#!/usr/bin/env python3
"""Логгер телеметрии ДТВ / СЭ-02м-3 → SQLite (USB → SD → eMMC).

  python3 scripts/sa02m_devices_logger.py
  STAND_DEVICES_LOG_INTERVAL_S=1 STAND_DEVICES_PURGE_EVERY_S=3600 ...
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sa02m_devices.device_history_db import (  # noqa: E402
    insert_mr_sample,
    insert_sample,
    purge_old,
    rotate_if_needed,
)
from sa02m_devices.device_history_migrate import promote_on_backend_change  # noqa: E402
from sa02m_devices.stand_devices import live_snapshot  # noqa: E402
from sa02m_devices.stand_storage_path import (  # noqa: E402
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

_stop = False


def _on_term(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True


def main() -> int:
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    last_purge = 0.0
    last_resolve = 0.0
    last_mr = 0.0
    prev_backend: str | None = None
    prev_path: Path | None = None
    target = resolve_storage_target(force_refresh=True)
    print(
        f"devices logger: backend={target.backend} path={target.active_path} "
        f"interval_usb_sd={INTERVAL_S}s interval_emmc={INTERVAL_EMMC_S}s "
        f"interval_mr={MR_INTERVAL_S}s purge_every={PURGE_EVERY_S}s",
        flush=True,
    )
    prev_backend = target.backend
    prev_path = target.active_path

    while not _stop:
        t0 = time.monotonic()
        interval = INTERVAL_EMMC_S if target.backend == "emmc" else INTERVAL_S
        interval = max(0.2, interval)
        try:
            now_m = time.monotonic()
            if now_m - last_resolve >= RESOLVE_EVERY_S:
                invalidate_resolve_cache()
                new_target = resolve_storage_target(force_refresh=True)
                if (
                    new_target.backend != target.backend
                    or new_target.active_path != target.active_path
                ):
                    report = promote_on_backend_change(
                        prev_backend,
                        prev_path,
                        new_target.backend,
                        new_target.active_path,
                    )
                    if report:
                        print(f"promote: {report}", flush=True)
                    print(
                        f"devices logger: switch {target.backend} → "
                        f"{new_target.backend} path={new_target.active_path}",
                        flush=True,
                    )
                    prev_backend = target.backend
                    prev_path = target.active_path
                    target = new_target
                else:
                    target = new_target
                last_resolve = now_m

            # Absorb eMMC staging whenever on removable (even without switch)
            if target.backend in ("usb", "sd"):
                report = promote_on_backend_change(
                    None, None, target.backend, target.active_path
                )
                if report and any(
                    s.get("action") in ("copy", "merge")
                    for s in (report.get("steps") or [])
                ):
                    print(f"promote: {report}", flush=True)

            snap = live_snapshot()
            try:
                from sa02m_devices.devices_widgets import filter_for_archive

                snap = filter_for_archive(snap)
            except Exception:  # noqa: BLE001
                pass
            insert_sample(snap, path=target.active_path)
            # MR-02m AI on its own (slower) cadence — same already-built snap,
            # no extra bus/MQTT I/O; only the SQLite write is added.
            if now_m - last_mr >= MR_INTERVAL_S:
                insert_mr_sample(snap, path=target.active_path)
                last_mr = now_m
            try:
                from sa02m_devices.device_events import detect_ce_events

                created = detect_ce_events(snap, path=target.active_path)
                for ev in created[:5]:
                    print(f"event: {ev.get('message')}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"events error: {exc}", file=sys.stderr, flush=True)
            rot = rotate_if_needed(path=target.active_path)
            if rot.get("rotated"):
                print(f"rotate: {rot}", flush=True)
            elif rot.get("skipped"):
                print(f"rotate skipped: {rot}", flush=True)

            now = time.time()
            if now - last_purge >= PURGE_EVERY_S:
                stats = purge_old(path=target.active_path)
                last_purge = now
                if stats["dtv_deleted"] or stats["ce_deleted"] or stats.get("mr_deleted"):
                    print(
                        f"purge: dtv={stats['dtv_deleted']} ce={stats['ce_deleted']} "
                        f"mr={stats.get('mr_deleted', 0)}",
                        flush=True,
                    )
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
            end = time.monotonic() + sleep_s
            while not _stop and time.monotonic() < end:
                time.sleep(min(0.2, end - time.monotonic()))
    print("devices logger stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
