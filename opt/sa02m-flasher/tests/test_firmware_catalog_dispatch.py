"""Compose the real offline catalog and Linux runner without serial I/O."""
from __future__ import annotations

import contextlib
import hashlib
import json
import struct
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
    def assert_manifest_refused_before_port(self, name, data, version, label, signatures=()):
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as stack:
            cache = Path(td)
            row = {"file": name, "version": version, "kind": "app", "device": label,
                   "signatures": signatures, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            (cache / name).write_bytes(data)
            (cache / ".index.json").write_text(json.dumps({"channels": {"stable": [row]}}))
            repo = make_repo(cache)
            entry = repo.get("stable", name)
            self.assertTrue(entry.downloaded)
            self.assertTrue(entry.flash_error)
            stack.enter_context(mock.patch.object(runner, "resolve_device_path", return_value="/dev/COM4"))
            lease = stack.enter_context(mock.patch.object(runner, "port_lease", side_effect=AssertionError("port acquired")))
            ctx = {"log": mock.Mock(), "progress": mock.Mock(), "cancel_evt": threading.Event()}
            for sig in ("12AI", "Sens.", "CE02M3"):
                self.assertNotIn(entry, repo.find_for_signature(sig))
                target = {"address": 15, "serial": 235546384, "signature": sig, "in_bootloader": True}
                for fast in (True, False):
                    params = {"firmware_channel": "stable", "firmware_file": name, "target": target,
                              "targets": [target], "use_fast_modbus": fast, "download_if_missing": False}
                    job = SimpleNamespace(port="COM4", params=params)
                    for run in (runner.run_flash_job, runner.run_flash_batch_job):
                        with self.subTest(label=label, target=sig, fast=fast, job=run.__name__):
                            with self.assertRaises(ValueError):
                                run(job, ctx, SimpleNamespace(mplc_stop_services=[]), repo)
            lease.assert_not_called()

    def test_manifest_cross_family_and_short_app_never_take_port(self):
        from tests.test_application_identity import packed
        raw = struct.pack("<II", 0x20008000, 0x08000807)
        name = "MR-02m_1.2.3.4.fw"
        data = catalog_image("app", "12AI")
        for label in ("MR-02m SENSOR", "RTU-Sensor MR-02m", "MR-02m-CE02M3"):
            self.assert_manifest_refused_before_port(name, data, "1.2.3.4", label)
        self.assert_manifest_refused_before_port(name, data, "1.2.3.4", "MR-02m", ["12AI", "Sens."])
        for ext, wire in ((".bin", raw), (".fw", packed(raw, b"12AI"))):
            self.assert_manifest_refused_before_port("MR-02m_1.2.3.4" + ext, wire, "1.2.3.4", "MR-02m")

    def test_extended_scan_retries_inferred_signature_and_marks_only_wire_evidence(self) -> None:
        from sa02m_flasher import scanner
        sn = 235581384  # Overlapping meter/10DICON production range.
        for responder, expected, inferred in ((sn, "CE02M3", False), (sn + 1, "10DICON", True)):
            with self.subTest(responder=responder), contextlib.ExitStack() as stack:
                raw = scanner.DeviceInfo(12, 115200, "N", 1, "", "1.0.0.1", "0.0.0.1", sn, False)
                guess = scanner._apply_signature_from_serial_if_missing(raw)
                self.assertTrue(guess.signature_inferred)
                def transfer(ser, request, **kwargs):
                    reg = int.from_bytes(request[8:10], "big")
                    payload = (b"".join(bytes((0, c)) for c in b"CE02M3".ljust(12, b"\0"))
                               if reg == 290 else bytes(16))
                    frame = b"\xfd\x46\x09" + responder.to_bytes(4, "big") + bytes((3, len(payload))) + payload
                    return frame + runner.modbus_rtu.crc16_modbus(frame).to_bytes(2, "little")
                for name, result in (("_wb_ext_scan", [(12, sn)]), ("scan_address", guess),
                                     ("open_port", mock.Mock()), ("_wb_ext_scan_host_preamble", None)):
                    stack.enter_context(mock.patch.object(scanner, name, return_value=result))
                bus = stack.enter_context(mock.patch.object(scanner, "send_receive", side_effect=transfer))
                _, _, devices = scanner._broadcast_probe_bauds("/dev/test", [(115200, "N", 1)])
                self.assertTrue(bus.called)
                self.assertEqual((devices[0].signature, devices[0].signature_inferred), (expected, inferred))

    def test_swapped_serial_words_require_a_valid_fast_reply_for_the_selected_sn(self) -> None:
        serial = 0x0E0A03E8
        for fast, responder, good_crc, passed in (
            (True, serial, True, True), (False, serial, True, False),
            (True, serial + 1, True, False), (True, 0, True, False), (True, serial, False, False),
        ):
            with self.subTest(fast=fast, responder=responder, good_crc=good_crc):
                def transfer(request):
                    offset = 8 if fast else 2
                    reg = int.from_bytes(request[offset:offset + 2], "big")
                    payload = (serial.to_bytes(4, "big") if reg == 270 else
                               b"".join(bytes((0, c)) for c in b"CE02M3".ljust(12, b"\0")))
                    prefix = (b"\xfd\x46\x09" + responder.to_bytes(4, "big") if fast else b"\xf7")
                    frame = prefix + bytes((3, len(payload))) + payload
                    crc = runner.modbus_rtu.crc16_modbus(frame) ^ int(not good_crc)
                    return frame + crc.to_bytes(2, "little")
                target = {"signature": "CE02M3", "serial": serial}
                identity, err = runner._confirm_flash_identity(
                    runner.fp.FlasherProtocol(transfer), target, 247, fast, image_family="CE-02m-3")
                self.assertEqual(err is None, passed, err)
                if passed:
                    self.assertEqual(identity, (serial, "CE-02m-3"))

    def test_verified_identity_gates_the_real_reset_and_program_paths(self) -> None:
        serial = 0x0E0A0101
        for fast in (False, True):
            for app, bl, bl_sn, allowed in (
                ("CE02M3", "CE02M3_bl", serial, True),
                ("CE02M3", "NONE", serial, True),
                ("CE02M3", "EN_METER_bl", serial, True),
                ("CE02M3", "NONE", serial + 1, False),
                ("CE02M3", "12AI_bl", serial, False),
                ("EN_METER", "CE02M3_bl", serial, False),
                (None, "NONE", serial, False),
                (None, "ENMETER", serial, False),
                (None, "CE02M3_bl", serial, True),
            ):
                with self.subTest(fast=fast, app=app, boot=bl, boot_sn=bl_sn):
                    def wire(signature, sn, addr):
                        proto = mock.Mock()
                        def read(target, reg, count):
                            self.assertEqual(target, serial if fast else addr)
                            if reg == 270:
                                self.assertEqual(count, 2)
                                return (sn & 65535).to_bytes(2, "big") + (sn >> 16).to_bytes(2, "big"), None
                            self.assertEqual((reg, count), (290, 12))
                            return b"".join(bytes((0, c)) for c in signature.encode().ljust(12, b"\0")), None
                        method = proto.read_holding_registers_by_serial if fast else proto.read_holding_registers
                        method.side_effect = read
                        proto.enter_bootloader_wb.return_value = None
                        proto.write_multiple_registers_by_serial.return_value = None
                        return proto

                    app_wire = wire(app or "", serial, 12)
                    bl_wire = wire(bl, bl_sn, 247)
                    device = {"address": 247 if app is None else 12, "signature": "CE02M3",
                              "serial": serial, "in_bootloader": app is None, "type_code": 14,
                              "application_identity": (serial, "CE-02m-3")}
                    with contextlib.ExitStack() as stack:
                        for name, value in {
                            "_make_flasher": (app_wire, mock.Mock()), "_open_bootloader_serial": mock.Mock(),
                            "_wait_bootloader_ready_on_serial": (True, None),
                            "_make_bootloader_flasher_protocol": bl_wire, "_post_reset_delays_before_bootloader_open": None,
                        }.items():
                            stack.enter_context(mock.patch.object(runner, name, return_value=value))
                        program = stack.enter_context(mock.patch.object(runner, "_flash_one_device", return_value=None))
                        err = runner._run_bootloader_flash_session(
                            "/dev/test", device, threading.Event(), mock.Mock(), mock.Mock(),
                            use_fast_modbus=fast, is_wb_firmware=False, is_bootloader_firmware=False,
                            duplicate_on_line=False, recovery=app is None, image=b"data", file_signature="NONE",
                            image_family="CE-02m-3",
                        )
                        self.assertEqual(err is None, allowed, err)
                        self.assertEqual(program.call_count, int(allowed))
                        if app == "CE02M3":
                            if fast:
                                app_wire.write_multiple_registers_by_serial.assert_called_once_with(serial, 129, [1])
                                app_wire.enter_bootloader_wb.assert_not_called()
                            else:
                                app_wire.enter_bootloader_wb.assert_called_once_with(12)
                        elif app == "EN_METER":
                            app_wire.enter_bootloader_wb.assert_not_called()
                            app_wire.write_multiple_registers_by_serial.assert_not_called()

    def test_catalog_runner_rejects_conflicting_families_and_guessed_targets(self) -> None:
        from sa02m_flasher import scanner
        device = scanner.DeviceInfo(247, 115200, "N", 1, "", "—", "0.0.0.1", 235581384, True)
        guessed = runner._device_to_dict(scanner._apply_signature_from_serial_if_missing(device))
        with tempfile.TemporaryDirectory() as td:
            entry = make_repo(Path(td)).add_upload(catalog_image("app"), "CE-02m-3_1.2.3.4.fw")
            for target in (guessed, {"signature": "EN_METER", "type_code": 100},
                           {"signature": "12AI"}, {"signature": "NONE", "in_bootloader": True}):
                self.assertIsNotNone(runner._resolve_flash_route_for_device(target, entry)[1], target)
            for code in (14, 100):
                target = {"signature": "CE02M3", "type_code": code}
                self.assertEqual(runner._resolve_flash_route_for_device(target, entry), (False, None))

    def test_container_header_is_checked_separately_from_catalog_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            entry = repo.add_upload(catalog_image("app", "12AI"), "confirmed.fw")
            entry.device = "CE-02m-3"
            params = {"firmware_channel": "local", "firmware_file": entry.file, "download_if_missing": False}
            with self.assertRaisesRegex(ValueError, "заголовка"):
                runner._load_firmware_for_flash(repo, params)
            alias = repo.add_upload(catalog_image("app", "EN_METER"), "unidentified.fw")
            self.assertEqual(alias.device, "")
            self.assertNotIn(alias, repo.find_for_signature("CE02M3"))
            params["firmware_file"] = alias.file
            with self.assertRaisesRegex(ValueError, "EN_METER"):
                runner._load_firmware_for_flash(repo, params)

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
            self.assertEqual(kwargs.pop("image_family"), family)
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
