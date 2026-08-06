# -*- coding: utf-8 -*-
"""
Regression: run_flash_batch_job must pause a bounded, cancellable inter-device
settle between two devices on the shared COM — a just-flashed MR module reboots
(~2 s) and is transiently at the bootloader's fixed addr 247, colliding with the
next device's enter_bootloader if flashed back-to-back (bench .135 cascade).

Covers: the settle runs N-1 times for N devices (never after the last), the
per-job override is clamped to a sane bound, and a cancel arriving during the
settle stops the batch. Plan .ai-dev/plans/flasher-batch-settle.md.
"""
from __future__ import annotations

import contextlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

# runner.py imports fcntl (Linux-only); stub for Windows test runs.
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")
    sys.modules["fcntl"].LOCK_EX = 2
    sys.modules["fcntl"].LOCK_NB = 4
    sys.modules["fcntl"].LOCK_UN = 8
    sys.modules["fcntl"].flock = lambda *a, **k: None

from sa02m_flasher import runner
from sa02m_flasher.firmware_repo import FirmwareEntry
from sa02m_flasher.jobs import Job, JobKind


class _Evt:
    """Minimal threading.Event stand-in with a scriptable is_set()."""

    def __init__(self, set_after: int = -1) -> None:
        # is_set() returns True once it has been queried `set_after` times
        # (set_after < 0 → never sets on its own).
        self._set = False
        self._queries = 0
        self._set_after = set_after

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        self._queries += 1
        if 0 <= self._set_after < self._queries:
            self._set = True
        return self._set


def _entry() -> FirmwareEntry:
    return FirmwareEntry(
        file="MR-02m_1.0.0.0.fw",
        version="1.0.0.0",
        signatures=["MR-02m-DI16"],
        device="MR-02m",
        size=1,
        sha256="",
        released="",
        notes="",
    )


@contextlib.contextmanager
def _yield_ctx(*_a, **_k):
    yield []


def _targets(n: int):
    return [
        {"address": 10 + i, "serial": i, "signature": "MR-02m-DI16"}
        for i in range(n)
    ]


def _run_batch(targets, *, cancel_evt=None, params_extra=None,
               settle_stub=None, flash_side_effect=None):
    """Drive run_flash_batch_job with the serial/firmware layers stubbed.

    Returns (flash_mock, settle_mock). All patched functions mirror the shapes
    the real batch loop consumes; nothing here opens a port or reads firmware.
    """
    cancel_evt = cancel_evt or _Evt()
    params = {"port": "COM4", "targets": targets}
    if params_extra:
        params.update(params_extra)
    job = Job(id="t", kind=JobKind.FLASH_BATCH, port="COM4", params=params)
    ctx = {
        "log": lambda *a, **k: None,
        "progress": lambda *a, **k: None,
        "cancel_evt": cancel_evt,
    }
    cfg = SimpleNamespace(mplc_stop_services=[])

    flash_mock = mock.Mock(side_effect=flash_side_effect, return_value=None)
    settle_mock = mock.Mock(side_effect=settle_stub)

    patches = [
        mock.patch.object(runner, "resolve_device_path", lambda *a, **k: "/dev/COM4"),
        mock.patch.object(runner, "_load_firmware_for_flash",
                          lambda *a, **k: (b"img", "MR-02m-DI16", "1.0", _entry())),
        mock.patch.object(runner, "port_lease", _yield_ctx),
        mock.patch.object(runner, "_port_flock", _yield_ctx),
        mock.patch.object(runner, "_resolve_flash_route_for_device",
                          lambda *a, **k: (False, None)),
        mock.patch.object(runner, "device_flash_route", lambda *a, **k: "mp_mr"),
        mock.patch.object(runner, "_firmware_signature_for_log", lambda *a, **k: "sig"),
        mock.patch.object(runner, "_duplicate_modbus_address_on_line", lambda *a, **k: False),
        mock.patch.object(runner, "_run_bootloader_flash_session", flash_mock),
        mock.patch.object(runner, "_settle_between_batch_devices", settle_mock),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        runner.run_flash_batch_job(job, ctx, cfg, None)
    return flash_mock, settle_mock


class TestBatchSettleCount(unittest.TestCase):
    def test_settle_between_each_pair_not_after_last(self) -> None:
        flash_mock, settle_mock = _run_batch(_targets(3))
        self.assertEqual(flash_mock.call_count, 3)
        # N-1 settles for N devices: after dev0 and dev1, never after dev2.
        self.assertEqual(settle_mock.call_count, 2)

    def test_single_device_no_settle(self) -> None:
        flash_mock, settle_mock = _run_batch(_targets(1))
        self.assertEqual(flash_mock.call_count, 1)
        self.assertEqual(settle_mock.call_count, 0)

    def test_failed_device_still_gets_the_gap(self) -> None:
        # dev0 fails (skip_on_error default True) → batch continues; the settle
        # still runs before dev1 and dev2.
        effects = [RuntimeError("addr 10 timeout"), None, None]

        def _flash(*_a, **_k):
            e = effects.pop(0)
            if isinstance(e, Exception):
                return str(e)  # session returns an error string, not a raise
            return None

        flash_mock, settle_mock = _run_batch(_targets(3), flash_side_effect=_flash)
        self.assertEqual(flash_mock.call_count, 3)
        self.assertEqual(settle_mock.call_count, 2)


class TestBatchSettleOverrideClamp(unittest.TestCase):
    def test_default_when_absent(self) -> None:
        self.assertEqual(
            runner._resolve_batch_settle_s({}),
            runner.BATCH_INTER_DEVICE_SETTLE_S,
        )

    def test_honours_valid_override(self) -> None:
        self.assertEqual(runner._resolve_batch_settle_s({"inter_device_settle_s": 5}), 5.0)
        self.assertEqual(runner._resolve_batch_settle_s({"inter_device_settle_s": 1.25}), 1.25)

    def test_clamps_high(self) -> None:
        self.assertEqual(
            runner._resolve_batch_settle_s({"inter_device_settle_s": 100}),
            runner.BATCH_INTER_DEVICE_SETTLE_MAX_S,
        )

    def test_clamps_negative_to_zero(self) -> None:
        self.assertEqual(runner._resolve_batch_settle_s({"inter_device_settle_s": -5}), 0.0)

    def test_invalid_falls_back_to_default(self) -> None:
        self.assertEqual(
            runner._resolve_batch_settle_s({"inter_device_settle_s": "abc"}),
            runner.BATCH_INTER_DEVICE_SETTLE_S,
        )


class TestBatchSettleCancel(unittest.TestCase):
    def test_cancel_during_settle_stops_batch(self) -> None:
        cancel_evt = _Evt()

        # Simulate a cancel arriving while settling between dev0 and dev1.
        def _cancel_during_settle(*_a, **_k):
            cancel_evt.set()

        flash_mock, settle_mock = _run_batch(
            _targets(3), cancel_evt=cancel_evt, settle_stub=_cancel_during_settle
        )
        # Only dev0 flashed; the settle fired once, then the batch broke.
        self.assertEqual(flash_mock.call_count, 1)
        self.assertEqual(settle_mock.call_count, 1)


class TestSettleHelperCancellation(unittest.TestCase):
    def test_preset_cancel_returns_without_logging_or_sleeping(self) -> None:
        logs = []
        with mock.patch.object(runner.time, "sleep") as sleep_mock:
            runner._settle_between_batch_devices(
                2.5, _Evt(set_after=0), lambda m, lvl: logs.append(m)
            )
        sleep_mock.assert_not_called()
        self.assertEqual(logs, [])

    def test_zero_settle_is_a_noop(self) -> None:
        logs = []
        with mock.patch.object(runner.time, "sleep") as sleep_mock:
            runner._settle_between_batch_devices(0.0, _Evt(), lambda m, lvl: logs.append(m))
        sleep_mock.assert_not_called()
        self.assertEqual(logs, [])

    def test_cancel_mid_wait_stops_early(self) -> None:
        # is_set(): guard(F) → log → while-cond(F) → sleep → while-cond(T) → stop.
        evt = _Evt(set_after=2)
        logs = []
        with mock.patch.object(runner.time, "sleep") as sleep_mock:
            runner._settle_between_batch_devices(2.5, evt, lambda m, lvl: logs.append(m))
        # Logged once, slept at most once before the cancel cut it short.
        self.assertEqual(len(logs), 1)
        self.assertLessEqual(sleep_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
