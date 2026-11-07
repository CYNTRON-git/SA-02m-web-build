"""Variant changes constrain the real profile, bus adapter and MQTT boundary."""
from pathlib import Path
import json
import os
from unittest import mock

from test_telemetry_hw_map import HwTestCase, tel


class TestVariantCapability(HwTestCase):
    def setUp(self):
        super().setUp()
        self.variant = Path(self._tmp.name) / "variant.conf"
        patcher = mock.patch.object(tel, "HW_VARIANT_FILE", str(self.variant), create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_conf()
        self.stub = self.make_client()
        self.hw = self.init_hw(self.stub)

    def select(self, value):
        self.variant.write_text("SA02M_HW_VARIANT=" + value + "\n")
        self.exp.writes.clear()
        self.exp.reads.clear()
        self.stub.published.clear()

    def test_changed_variant_refuses_direct_command_before_bus(self):
        self.select("sa02m-2eth")
        ok, why = self.hw.set_channel("do", True)
        self.assertFalse(ok)
        self.assertIn("variant", why)
        self.assertEqual(self.exp.writes, [])
        self.assertEqual(self.exp.reads, [])
        self.assertNotIn("do", self.hw.channels())
        self.assertEqual(self.hw._profile.output_mask(), 7)
        self.assertEqual(self.hw._profile.extra_output_mask, 8)

    def test_callback_and_poll_observe_variant_without_reconstruction(self):
        self.select("sa02m-2eth")
        self.send(self.stub, "do", b"1")
        self.assertEqual(self.stub.published, [])
        self.assertEqual(self.exp.writes, [])
        levels, why = self.hw.read_channels()
        self.assertEqual(set(levels), {"beeper", "alarm_led"})
        self.select("sa02m-1eth")
        self.send(self.stub, "do", b"1")
        self.assertEqual(self.stub.published, [("controls/do", "1")])

    def test_invalid_variant_refuses_do_but_preserves_other_channels(self):
        self.select("unrecognised")
        self.assertFalse(self.hw.set_channel("do", True)[0])
        self.assertTrue(self.hw.set_channel("beeper", True)[0])
        self.assertEqual(set(self.hw.read_channels()[0]), {"beeper", "alarm_led"})

    def test_shared_variant_matrix(self):
        matrix = json.loads((Path(__file__).resolve().parents[3] / "scripts/dev/fixtures/hardware-variants.json").read_text())
        for case in matrix:
            with self.subTest(case=case["name"]):
                if self.variant.is_dir():
                    self.variant.rmdir()
                elif self.variant.exists() or self.variant.is_symlink():
                    self.variant.unlink()
                kind = case.get("kind")
                if kind == "directory":
                    self.variant.mkdir()
                elif kind == "symlink":
                    if os.name == "nt":
                        with mock.patch.object(tel.os.path, "lexists", return_value=True):
                            self.assertEqual(tel._hw_variant(), case["variant"])
                        continue
                    self.variant.symlink_to(self.variant.parent / "absent")
                elif kind != "absent":
                    self.variant.write_bytes(case["text"].encode("utf-8") + bytes.fromhex(case.get("suffix_hex", "")))
                if kind == "unreadable":
                    with mock.patch("builtins.open", side_effect=PermissionError):
                        self.assertEqual(tel._hw_variant(), case["variant"])
                else:
                    self.assertEqual(tel._hw_variant(), case["variant"])

    def test_invalid_variant_bytes_refuse_direct_and_callback_before_bus(self):
        matrix = json.loads((Path(__file__).resolve().parents[3] / "scripts/dev/fixtures/hardware-variants.json").read_text())
        for case in matrix:
            if case.get("kind") or case["variant"] != "unknown":
                continue
            with self.subTest(case=case["name"]):
                self.variant.write_bytes(case["text"].encode("utf-8") + bytes.fromhex(case.get("suffix_hex", "")))
                self.exp.reads.clear()
                self.exp.writes.clear()
                self.stub.published.clear()
                self.assertFalse(self.hw.set_channel("do", True)[0])
                self.send(self.stub, "do", b"1")
                self.assertEqual(self.exp.reads, [])
                self.assertEqual(self.exp.writes, [])
                self.assertEqual(self.stub.published, [])

    def test_mqtt_owned_tombstones_and_metadata_return_on_variant_change(self):
        mqtt = mock.Mock()
        with mock.patch.object(tel.mqtt, "Client", return_value=mqtt), mock.patch.object(tel, "_resolve_device_id", return_value=("variant-bench", "fixture")):
            client = tel.TelemetryClient()
        client._hw = self.hw
        prefix = f"{tel.DEVICE_BASE}/variant-bench/"

        def pubs():
            return [(call.args[0].removeprefix(prefix), call.args[1]) for call in mqtt.publish.call_args_list]

        client._publish_meta()
        self.assertIn(("controls/do/meta/type", "switch"), pubs())
        mqtt.publish.reset_mock()
        self.select("sa02m-2eth")
        client._publish_meta()
        cleared = [suffix for suffix, value in pubs() if value == ""]
        self.assertEqual(cleared, ["controls/do", "controls/do/meta/type"])
        self.assertNotIn(("controls/do/meta/type", "switch"), pubs())
        self.assertIn(("controls/beeper/meta/type", "switch"), pubs())
        mqtt.publish.reset_mock()
        with mock.patch.object(tel, "cpu_usage_pct", return_value=0), mock.patch.object(tel, "cpu_temp_c", return_value=0), mock.patch.object(tel, "ram_pct", return_value=0), mock.patch.object(tel, "uptime_s", return_value=0), mock.patch.object(tel, "rs485_stats", return_value=None):
            client._publish_metrics()
            self.assertNotIn("controls/do", [suffix for suffix, _ in pubs()])
            self.assertIn("controls/beeper", [suffix for suffix, _ in pubs()])
            mqtt.publish.reset_mock()
            self.select("sa02m-1eth")
            client._publish_meta()
            client._publish_metrics()
        self.assertIn(("controls/do/meta/type", "switch"), pubs())
        self.assertIn("controls/do", [suffix for suffix, _ in pubs()])
        self.assertEqual(self.exp.writes, [])
        for call in mqtt.publish.call_args_list:
            self.assertTrue(call.args[0].startswith(prefix))
            self.assertTrue(call.kwargs["retain"])
        self.select("sa02m-2eth")
        for _ in range(2):
            mqtt.publish.reset_mock()
            client._on_connect(mqtt, None, {}, 0)
            self.assertEqual([suffix for suffix, value in pubs() if value == "" and suffix.startswith("controls/do")], ["controls/do", "controls/do/meta/type"])

    def test_one_capability_read_per_command_and_poll(self):
        with mock.patch.object(tel, "_hw_variant", wraps=tel._hw_variant) as reader:
            self.send(self.stub, "do", b"1")
            self.assertEqual(reader.call_count, 1)
            reader.reset_mock()
            self.hw.read_channels()
            self.assertEqual(reader.call_count, 1)
