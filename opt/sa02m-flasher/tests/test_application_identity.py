"""Exercise the same raw image and packed release paths as the local catalogue."""
import hashlib
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from sa02m_flasher import firmware
from sa02m_flasher.firmware_repo import FirmwareRepo


def raw_image(tag=b"", version=b"1.2.3.4"):
    image = bytearray(b"\xff" * 48000)
    struct.pack_into("<II", image, 0, firmware.RAM_END, firmware.FLASH_APP_START + 9)
    image[8:10] = b"\x00\xbf"
    image[64:68] = bytes((9, 0, 1, 8))
    image[80:88] = b"2.2.3.4\0"
    if tag:
        marker = tag + b"_VER:" + version + b"\0"
        image[37001:37001 + len(marker)] = marker
    return bytes(image)


def packed(image, sig=b"NONE"):
    data = bytearray(image)
    data[::2], data[1::2] = image[1::2], image[::2]
    return sig.ljust(12, b"\0") + struct.pack("<I", len(data)) + bytes(16) + data


class ApplicationIdentity(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.directory = Path(td.name)
        self.repo = FirmwareRepo(self.directory, "https://invalid.invalid/index", "https://invalid.invalid/")

    def test_reset_cannot_point_inside_vector_words_or_a_partial_instruction(self):
        from unittest.mock import Mock
        from sa02m_flasher import flash_protocol as fp
        for target, tail in ((0, b""), (2, b""), (4, b""), (6, b""), (8, b""), (8, b"\x00\xf0")):
            raw = struct.pack("<II", firmware.RAM_END, firmware.FLASH_APP_START + target + 1) + tail
            for wire in (raw, packed(raw, b"12AI")):
                for sequence in (fp.run_flash_sequence_by_address, fp.run_flash_sequence):
                    with self.subTest(target=target, tail=tail, sequence=sequence.__name__, size=len(wire)):
                        bus = Mock()
                        self.assertIn("Reset_Handler", sequence(bus, 247, wire, "12AI"))
                        self.assertEqual(bus.mock_calls, [])

    def test_markerless_or_interrupted_reimport_preserves_valid_cached_release(self):
        from unittest.mock import patch
        name = "RTU-Sensor_1.2.3.4.fw"
        raw = bytearray(raw_image())
        struct.pack_into("<I", raw, 4, firmware.FLASH_APP_START + 9)
        raw[8:10] = b"\x00\xbf"
        good, wrong = packed(raw, b"Sens."), packed(raw, b"RGBW_WS2812")
        first = self.repo.add_upload(good, name)
        expected = first.to_dict()
        self.assertEqual(first.flash_error, "")
        for _ in range(2):
            self.assertEqual(self.repo.add_upload(good, name).to_dict(), expected)
            with self.assertRaises(ValueError):
                self.repo.add_upload(wrong, name)
            with patch.object(self.repo, "_entry_from_file", side_effect=OSError("read failed")):
                with self.assertRaises(OSError):
                    self.repo.add_upload(wrong, name)
            with patch.object(Path, "replace", side_effect=OSError("rename failed")):
                with self.assertRaises(OSError):
                    self.repo.add_upload(good, name)
            self.assertEqual((self.directory / name).read_bytes(), good)
            self.assertEqual(self.repo.get("local", name).to_dict(), expected)
            self.repo = FirmwareRepo(self.directory, "", "")
            self.assertEqual(self.repo.get("local", name).to_dict(), expected)

    def test_racing_uploads_and_cold_reader_only_see_complete_valid_releases(self):
        import concurrent.futures
        import threading
        from unittest.mock import patch
        name = "RTU-Sensor_1.2.3.4.fw"
        first = packed(raw_image(), b"Sens.")
        second = packed(raw_image().replace(b"2.2.3.4", b"3.3.3.4"), b"Sens.")
        wrong = packed(raw_image(), b"RGBW_WS2812")
        self.repo.add_upload(first, name)
        opened, resume = threading.Event(), threading.Event()
        def interrupted_write(path, data):
            with path.open("wb") as handle:
                handle.write(data[:32])
                handle.flush()
                if data == second:
                    opened.set()
                    if not resume.wait(10):
                        raise TimeoutError("staging test timed out")
                return 32 + handle.write(data[32:])
        with concurrent.futures.ThreadPoolExecutor(2) as pool, patch.object(Path, "write_bytes", interrupted_write):
            upload = pool.submit(self.repo.add_upload, second, name)
            try:
                self.assertTrue(opened.wait(10))
                cold = FirmwareRepo(self.directory, "", "")
                self.assertEqual([(e.file, e.flash_error) for e in cold.list_entries()], [(name, "")])
                self.assertEqual((self.directory / name).read_bytes(), first)
                with self.assertRaises(ValueError):
                    pool.submit(self.repo.add_upload, wrong, name).result(5)
            finally:
                resume.set()
            self.assertEqual(upload.result(5).sha256, hashlib.sha256(second).hexdigest())
        cold = FirmwareRepo(self.directory, "", "")
        self.assertEqual(cold.get("local", name).to_dict(), self.repo.get("local", name).to_dict())
        self.assertEqual((self.directory / name).read_bytes(), second)

    def test_entire_catalog_family_label_must_identify_one_native_family(self):
        from sa02m_flasher import module_profiles as mp
        from sa02m_flasher.firmware_repo import FirmwareEntry, confirmed_version
        for family, signature in (("MR-02m DI16", "12AI"), ("MP02m", "12AI"),
                                  ("Sens.", "Sens."), ("RTU-Sensor", "Sens."), ("CE02M3", "CE02M3")):
            canonical = mp.manifest_device_for_signature(signature)
            with self.subTest(device=family):
                self.assertEqual(mp.manifest_device_family(family), canonical)
                entry = FirmwareEntry(file="manual.fw", device=family, version="1.2.3.4")
                self.assertEqual(confirmed_version(entry, signature, "1.2.3.4", None), "1.2.3.4")
        for label in ("MR-02m SENSOR", "MR-02m-SENSOR", "RTU-Sensor MR-02m", "CE-02m-3 12AI",
                      "MR-02m-CE02M3", "MR-02m LED", "otherSENSOR", "RTU-Sensor-unknown"):
            with self.subTest(device=label):
                self.assertIsNone(mp.manifest_device_family(label))
                for sig in ("12AI", "Sens.", "CE02M3"):
                    entry = FirmwareEntry(file="manual.fw", device=label, version="1.2.3.4")
                    with self.assertRaises(ValueError):
                        confirmed_version(entry, sig, "1.2.3.4", None)

    def test_manifest_upload_preserves_usable_release_and_channel_metadata(self):
        name = "RTU-Sensor_1.2.3.4.fw"
        good, bad = packed(raw_image(), b"Sens."), packed(raw_image(), b"RGBW_WS2812")
        for digest in ("", hashlib.sha256(good).hexdigest()):
            with self.subTest(digest=digest), tempfile.TemporaryDirectory() as td:
                cache = Path(td)
                row = {"file": name, "device": "RTU-Sensor", "version": "1.2.3.4", "sha256": digest}
                pi = {"file": "PI-02m.fw", "device": "PI-02m", "version": "1.0.0.1"}
                (cache / ".index.json").write_text(json.dumps({"channels": {"stable": [row, pi], "beta": [row]}}))
                repo = FirmwareRepo(cache, "", "")
                accepted = repo.add_upload(good, name)
                self.assertEqual((accepted.channel, accepted.flash_error), ("stable", ""))
                snapshot = [entry.to_dict() for entry in repo.list_entries()]
                self.assertEqual(len(snapshot), 3)
                self.assertTrue(repo.get("stable", pi["file"]).flash_error)
                with self.assertRaises(ValueError):
                    repo.add_upload(bad, name)
                self.assertEqual((cache / name).read_bytes(), good)
                self.assertEqual([entry.to_dict() for entry in repo.list_entries()], snapshot)
                cold = FirmwareRepo(cache, "", "")
                self.assertEqual([entry.to_dict() for entry in cold.list_entries()], snapshot)

    def test_all_application_markers_in_unversioned_and_packed_images(self):
        for tag, family in ((b"MR02M", "MR-02m"), (b"DTV", "RTU-Sensor"),
                            (b"LED", "LED"), (b"CE02M3", "CE-02m-3")):
            raw = raw_image(tag)
            for suffix, data in (("bin", raw), ("fw", packed(raw))):
                with self.subTest(family=family, suffix=suffix):
                    name = tag.decode() + "." + suffix
                    entry = self.repo.add_upload(data, name)
                    self.assertEqual((entry.version, entry.device), ("1.2.3.4", family))
                    self.assertEqual(firmware.load_firmware(self.directory / name)[2], "1.2.3.4")

    def test_incidental_numbers_never_identify_an_anonymous_binary(self):
        raw = raw_image()
        self.assertIsNone(firmware.parse_version_from_image(raw))
        self.assertIsNone(firmware.parse_version_from_image(raw.replace(b"2.2.3.4", b"padding")))
        entry = self.repo.add_upload(raw, "upload.bin")
        self.assertEqual((entry.version, entry.device), ("?", ""))
        self.assertEqual(firmware.load_firmware(self.directory / "upload.bin")[2], "?")
        for name, version in (("RTU-Sensor_1.0.2.8.bin", "1.0.2.8"),
                              ("RGBW_WS2812_1.0.3.2.bin", "1.0.3.2")):
            self.assertEqual(self.repo.add_upload(raw, name).version, version)

    def test_wire_sequences_refuse_wrong_vectors_before_any_modbus_request(self):
        from unittest.mock import Mock
        from sa02m_flasher import flash_protocol as fp
        raw = bytearray(firmware.BL_IMAGE_TOTAL_BYTES)
        for kind, sequence in (("bootloader", fp.run_flash_bootloader_sequence_by_address),
                               ("bootloader", fp.run_flash_sequence_bootloader),
                               ("app", fp.run_flash_sequence_by_address), ("app", fp.run_flash_sequence)):
            destination = firmware.FLASH_APP_START if kind == "bootloader" else firmware.BL_CODE_ORIGIN
            struct.pack_into("<II", raw, 0, firmware.RAM_END, destination | 1)
            for wire in (bytes(raw), packed(raw)):
                with self.subTest(sequence=sequence.__name__, size=len(wire)):
                    device = Mock()
                    self.assertIn("Reset_Handler", sequence(device, 247, wire, "12AI"))
                    self.assertEqual(device.mock_calls, [])

    def test_wire_format_matches_legacy_empty_header_and_raw_crc2_collision(self):
        from sa02m_flasher import flash_protocol as fp
        raw = bytearray(raw_image(b"MR02M"))
        struct.pack_into("<I", raw, 12, 1024)
        raw[16:20] = b"CRC2"
        self.assertFalse(fp._image_has_embedded_fw_header(raw))
        self.assertTrue(fp._image_has_embedded_fw_header(packed(raw, b"")))
        for wire in (bytes(raw), packed(raw, b"")):
            self.assertIsNone(fp._native_vector_error(wire, "app"))

    def test_conflicting_explicit_identity_never_replaces_a_valid_upload(self):
        name = "RTU-Sensor_1.2.3.4.fw"
        good = packed(raw_image(b"DTV"), b"Sens.")
        self.repo.add_upload(good, name)
        for data in (packed(raw_image(b"DTV", b"1.2.3.5")),
                     packed(raw_image(b"MR02M")), packed(raw_image(b"DTV"), b"CE02M3"),
                     packed(raw_image(b"DTV").replace(b"2.2.3.4\0", b"LED_VER:9.9.9.9\0"))):
            with self.subTest(prefix=data[:12]):
                with self.assertRaises(ValueError):
                    self.repo.add_upload(data, name)
                self.assertEqual((self.directory / name).read_bytes(), good)

    @unittest.skipIf(sys.platform == "win32", "The production runner imports Linux fcntl")
    def test_runner_rechecks_actual_bytes_and_rejects_unidentified_local_binary(self):
        from sa02m_flasher import runner
        name = "upload.bin"
        params = {"firmware_channel": "local", "firmware_file": name, "download_if_missing": False}
        entry = self.repo.add_upload(raw_image(), name)
        entry.device = "MR-02m"  # Cache entry from a previous installation.
        with self.assertRaisesRegex(ValueError, "BIN"):
            runner._load_firmware_for_flash(self.repo, params)
        entry.source, entry.device, entry.version = "manifest", "RTU-Sensor", "1.0.2.8"
        self.assertEqual(runner._load_firmware_for_flash(self.repo, params)[2], "1.0.2.8")
        entry.defaulted_manifest_device = True
        with self.assertRaisesRegex(ValueError, "BIN"):
            runner._load_firmware_for_flash(self.repo, params)
        entry.defaulted_manifest_device, entry.sha256 = False, ""
        with self.assertRaisesRegex(ValueError, "BIN"):
            runner._load_firmware_for_flash(self.repo, params)
        # Start a new process without the deliberately mutated manifest row.
        self.repo = FirmwareRepo(self.directory, "", "")
        entry = self.repo.add_upload(raw_image(b"DTV"), name)
        image, _, version, loaded = runner._load_firmware_for_flash(self.repo, params)
        self.assertEqual((version, loaded.device), ("1.2.3.4", "RTU-Sensor"))
        self.assertTrue(image.startswith(raw_image()[:8]))
        for family, version in (("MR-02m", "1.2.3.4"), ("RTU-Sensor", "9.9.9.9")):
            entry.device, entry.version = family, version
            with self.assertRaisesRegex(ValueError, "маркер"):
                runner._load_firmware_for_flash(self.repo, params)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            firmware.load_identified_firmware(self.directory / name, expected_sha256="0" * 64)

    def test_cold_cache_keeps_conflicts_out_of_the_catalogue(self):
        (self.directory / "MR-02m_1.2.3.4.fw").write_bytes(packed(raw_image(b"DTV")))
        self.assertEqual(self.repo.list_entries(), [])
        self.assertTrue((self.directory / "MR-02m_1.2.3.4.fw").is_file())

    def test_markerless_foreign_headers_remain_visible_but_are_not_native_candidates(self):
        for sig, family in ((b"RGBW_WS2812", "LED"), (b"RGBWWS2812", "LED"),
                            (b"RGBW", "LED"), (b"LED", "LED"), (b"WB-MLED3", "WB"),
                            (b"ledGe", "WB"), (b"mr6c_v2", "WB"), (b"MAO4", "WB"),
                            (b"CRSTDm_AHU", "Carel")):
            for name in ("manual.fw", "MR-02m_1.0.3.2.fw"):
                with self.subTest(signature=sig, filename=name):
                    entry = self.repo.add_upload(packed(raw_image(), sig), name)
                    self.assertEqual(entry.device, family)
                    if name == "manual.fw":
                        self.assertIn(f"{family} не допускает нативную запись", entry.flash_error)
                    else:
                        self.assertIn("не совпадает с именем", entry.flash_error)
                    self.assertNotIn(entry, self.repo.find_for_signature("12AI"))

    def test_download_and_cached_manifest_cannot_authorize_foreign_or_mislabeled_bytes(self):
        from unittest.mock import patch
        from sa02m_flasher.firmware_repo import load_for_flash
        for name, payload, family, version, allowed in (
                ("manual.fw", packed(raw_image(), b"RGBW"), "MR-02m", "1.0.3.2", False),
                ("RTU-Sensor_1.0.2.8.bin", raw_image(), "RTU-Sensor", "9.9.9.9", False),
                ("unsigned.bin", raw_image(), "RTU-Sensor", "1.0.2.8", True)):
            row = {"file":name, "device":family, "version":version, "kind":"app",
                   "size":len(payload), "sha256":hashlib.sha256(payload).hexdigest()}
            manifest = {"schema":1, "channels":{"stable":[row]}}
            (self.directory / ".index.json").write_text(json.dumps(manifest))
            for cold in (False, True):
                with self.subTest(name=name, cold=cold):
                    repo = FirmwareRepo(self.directory, "https://invalid.invalid/index", "https://invalid.invalid/")
                    entry = repo.get("stable", name)
                    with patch("sa02m_flasher.firmware_repo._http_get", return_value=payload) as download:
                        path = repo.ensure_local_path(entry)
                    self.assertEqual(download.call_count, 0 if cold else 1)
                    self.assertIs(bool(entry.flash_error), not allowed)
                    if allowed:
                        self.assertEqual(load_for_flash(entry, path)[2], version)
                    else:
                        with self.assertRaises(ValueError):
                            load_for_flash(entry, path)

    def test_full_bootloader_dump_extracts_only_vectors_and_code_after_hash_check(self):
        from sa02m_flasher.firmware_repo import load_for_flash
        data = bytearray(b"\xff" * 0x40000)
        struct.pack_into("<II", data, 0, firmware.RAM_END, firmware.BL_CODE_ORIGIN | 1)
        data[0x38000:0x38002] = b"\x00\xbf"
        data[0x800:0x810] = b"DTV_VER:1.2.3.4\0"
        name = "MR-02m_bootloader_0.0.0.35.bin"
        entry = self.repo.add_upload(bytes(data), name)
        entry.source, entry.defaulted_manifest_device = "manifest", False
        actual, _, version = load_for_flash(entry, self.directory / name)
        self.assertEqual(version, "0.0.0.35")
        self.assertEqual(actual, bytes(data[:0x800] + data[0x38000:0x40000]))

    @unittest.skipIf(sys.platform == "win32", "The production runner imports Linux fcntl")
    def test_packed_identity_is_rechecked_before_both_job_types_take_the_port(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from sa02m_flasher import runner
        ctx = {"log": Mock(), "progress": Mock(), "cancel_evt": Mock()}
        target = {"address": 12, "serial": 235546384, "signature": "12AI"}
        cases = [(name, packed(raw_image(), sig)) for sig in (b"LED", b"WB-MLED3", b"Sens.", b"CE02M3")
                 for name in ("manual.fw", "MR-02m_1.0.3.2.fw")]
        cases.extend((name, raw_image()) for name in ("MR-02m_1..2.bin", "MR-02m_....bin"))
        cases.append(("manual.fw", packed(raw_image())))
        for name, data in cases:
            with self.subTest(name=name, header=data[:12]), tempfile.TemporaryDirectory() as cache:
                repo = FirmwareRepo(Path(cache), "", "")
                entry = repo.add_upload(data, name)
                entry.device = "MR-02m"  # Saved before classifiers were corrected.
                params = {"firmware_channel": "local", "firmware_file": name, "download_if_missing": False,
                          "target": target, "targets": [target]}
                job = SimpleNamespace(port="COM4", params=params)
                with patch.object(runner, "resolve_device_path", return_value="/dev/COM4"), \
                        patch.object(runner, "port_lease", side_effect=AssertionError("port acquired")) as lease:
                    for run in (runner.run_flash_job, runner.run_flash_batch_job):
                        with self.assertRaises((ValueError, RuntimeError)):
                            run(job, ctx, SimpleNamespace(mplc_stop_services=[]), repo)
                    lease.assert_not_called()

    @unittest.skipIf(sys.platform == "win32", "The production runner imports Linux fcntl")
    def test_unnamed_or_markerless_evidence_must_match_the_selected_release(self):
        from sa02m_flasher import runner
        for filename, payload, family, version in (
              ("RTU-Sensor_1.0.2.8.bin", raw_image(), "RTU-Sensor", "9.9.9.9"),
              ("RTU-Sensor_1.0.2.8.fw", packed(raw_image(), b"Sens."), "RTU-Sensor", "9.9.9.9"),
              ("RTU-Sensor_1.0.2.8.fw", packed(raw_image(), b"12AI"), "MR-02m", "1.0.2.8")):
            with self.subTest(filename=filename, family=family), tempfile.TemporaryDirectory() as cache:
                repo = FirmwareRepo(Path(cache), "", "")
                entry = repo.add_upload(payload, filename)
                entry.device, entry.version = family, version
                params = {"firmware_channel":"local","firmware_file":filename,"download_if_missing":False}
                with self.assertRaises(ValueError):
                    runner._load_firmware_for_flash(repo, params)

    @unittest.skipIf(sys.platform == "win32", "The production runner imports Linux fcntl")
    def test_vector_table_must_match_declared_kind_before_any_uart_access(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from sa02m_flasher import runner
        raw = bytearray(raw_image(b"MR02M")[:firmware.BL_IMAGE_TOTAL_BYTES])
        raw[2100] = 1
        for boot_name in (False, True):
            reset = firmware.FLASH_APP_START if boot_name else firmware.BL_CODE_ORIGIN
            struct.pack_into("<II", raw, 0, firmware.RAM_END, reset | 1)
            for suffix, payload in (("fw", packed(raw, b"12AI")), ("bin", bytes(raw))):
                with self.subTest(boot_name=boot_name, suffix=suffix):
                    filename = "MR-02m_" + ("bootloader_" if boot_name else "") + "1.2.3.4." + suffix
                    self.repo.add_upload(payload, filename)
                    target = {"address": 12, "serial": 235546384, "signature": "12AI"}
                    job = SimpleNamespace(port="COM4", params={"firmware_file":filename,
                        "firmware_channel":"local","target":target,"targets":[target]})
                    ctx = {"log":Mock(),"progress":Mock(),"cancel_evt":Mock()}
                    with patch.object(runner, "resolve_device_path", return_value="/dev/COM4"), \
                            patch.object(runner, "port_lease", side_effect=AssertionError("port acquired")):
                        for run in (runner.run_flash_job, runner.run_flash_batch_job):
                            with self.assertRaisesRegex(ValueError, "вектор|Reset_Handler"):
                                run(job, ctx, SimpleNamespace(mplc_stop_services=[]), self.repo)
