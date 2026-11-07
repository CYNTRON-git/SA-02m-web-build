"""Compose the real offline catalog and Linux runner without serial I/O."""
from __future__ import annotations

import contextlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_firmware_catalog_metadata import catalog_image, make_repo

if sys.platform != "win32":
    from sa02m_flasher import runner
    from sa02m_flasher.jobs import Job, JobKind


@unittest.skipIf(sys.platform == "win32", "Real runner import requires Linux fcntl; run this suite in WSL")
class TestCatalogRunnerDispatch(unittest.TestCase):
    def _exercise(self, *, batch: bool) -> None:
        for family, signature in (("RTU-Sensor", "Sens."), ("CE-02m-3", "CE02M3"), ("MR-02m", "12AI")):
            for kind in ("bootloader", "app"):
                for fast in (False, True):
                    with self.subTest(family=family, kind=kind, fast=fast):
                        self._dispatch(family, signature, kind, fast, batch)

    def _dispatch(self, family, signature, kind, fast, batch) -> None:
        name = f"{family}_{'bootloader_' if kind == 'bootloader' else ''}1.2.3.4.fw"
        data = catalog_image(kind)
        targets = [{"address": 10 + i, "serial": 100 + i, "signature": signature}
                   for i in range(2 if batch else 1)]
        params = {"port": "COM4", "firmware_channel": "local", "firmware_file": name,
                  "download_if_missing": False, "use_fast_modbus": fast, "inter_device_settle_s": 0}
        params["targets" if batch else "target"] = targets if batch else targets[0]
        job = Job(id="catalog-test", kind=JobKind.FLASH_BATCH if batch else JobKind.FLASH, port="COM4", params=params)
        progress = mock.Mock()
        ctx = {"log": mock.Mock(), "progress": progress, "cancel_evt": threading.Event()}
        cfg = SimpleNamespace(mplc_stop_services=[])
        session_flags = []
        sequence_calls = []
        flasher = mock.Mock(spec=["jump_to_app", "jump_to_app_by_serial"])
        flasher.jump_to_app.return_value = None
        flasher.jump_to_app_by_serial.return_value = None

        def session(device_path, target, cancel_evt, log_cb, progress_cb, **kwargs):
            session_flags.append(kwargs["is_bootloader_firmware"])
            return runner._flash_one_device(
                flasher, target, kwargs.pop("image"), kwargs.pop("file_signature"),
                ser=None, cancel_evt=cancel_evt, log_cb=log_cb, progress_cb=progress_cb, **kwargs,
            )

        def sequence(label):
            def record(*args, **kwargs):
                sequence_calls.append(label)
                self.assertEqual(args[2], data)
                self.assertEqual(args[3], signature)
                return None
            return record

        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("sa02m_flasher.firmware_repo._http_get", side_effect=AssertionError("Unexpected network")))
            cache = Path(td)
            make_repo(cache).add_upload(data, name)
            repo = make_repo(cache)
            loaded = runner._load_firmware_for_flash(repo, params)
            self.assertEqual(loaded[0], data)
            self.assertEqual(loaded[1], "NONE")
            self.assertEqual(loaded[2], "1.2.3.4")
            self.assertIs(loaded[3], repo.get("local", name))
            stack.enter_context(mock.patch.object(runner, "resolve_device_path", return_value="/dev/COM4"))
            stack.enter_context(mock.patch.object(runner, "port_lease", side_effect=lambda *a: contextlib.nullcontext()))
            stack.enter_context(mock.patch.object(runner, "_port_flock", side_effect=lambda *a: contextlib.nullcontext()))
            stack.enter_context(mock.patch.object(runner, "open_port", side_effect=AssertionError("Unexpected hardware I/O")))
            stack.enter_context(mock.patch.object(runner, "_run_bootloader_flash_session", side_effect=session))
            stack.enter_context(mock.patch.object(runner, "_preflight_read_bootloader_info_by_address_mr", return_value=signature))
            stack.enter_context(mock.patch.object(runner, "_resolve_bootloader_serial_mr_style", return_value=(100, None)))
            stack.enter_context(mock.patch.object(runner, "_preflight_read_bootloader_info_by_serial_mr", return_value=(signature, None)))
            stack.enter_context(mock.patch.object(runner.time, "sleep"))
            for function, label in (
                ("run_flash_bootloader_sequence_by_address", "bootloader/address"),
                ("run_flash_sequence_by_address", "app/address"),
                ("run_flash_sequence_bootloader", "bootloader/serial"),
                ("run_flash_sequence", "app/serial"),
            ):
                stack.enter_context(mock.patch.object(runner.fp, function, side_effect=sequence(label)))
            if batch:
                runner.run_flash_batch_job(job, ctx, cfg, repo)
                self.assertNotIn("errors", job.params)
            else:
                runner.run_flash_job(job, ctx, cfg, repo)
            self.assertEqual(sequence_calls, [f"{kind}/{'serial' if fast else 'address'}"] * len(targets))
            self.assertEqual(session_flags, [kind == "bootloader"] * len(targets))
            self.assertEqual((cache / name).read_bytes(), data)
            self.assertEqual(progress.call_args.args[0], 100)
            if kind == "bootloader":
                flasher.jump_to_app.assert_not_called()
                flasher.jump_to_app_by_serial.assert_not_called()
            else:
                jump = flasher.jump_to_app_by_serial if fast else flasher.jump_to_app
                self.assertEqual(jump.call_count, len(targets))

    def test_single_job_uses_catalog_kind_for_actual_sequence(self) -> None:
        self._exercise(batch=False)

    def test_batch_job_uses_catalog_kind_for_actual_sequence(self) -> None:
        self._exercise(batch=True)


if __name__ == "__main__":
    unittest.main()
