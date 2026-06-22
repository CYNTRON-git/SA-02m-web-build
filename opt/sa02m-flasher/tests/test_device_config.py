from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from sa02m_flasher import device_config, module_profiles


class TestDeviceConfig(unittest.TestCase):
    def test_resolve_kind_falls_back_to_scan_device_record(self) -> None:
        identity = {
            "signature": "",
            "serial": 0,
            "app_version": "—",
            "bootloader_version": "—",
            "type_code": None,
        }
        device = {
            "signature": "MR-02m 6AI6AO",
            "module_type": module_profiles.MP02_AO6AI6,
        }
        kind, patched = device_config._resolve_kind(identity, device)
        self.assertEqual(kind, "mr")
        self.assertEqual(patched["signature"], "MR-02m 6AI6AO")
        self.assertEqual(patched["type_code"], module_profiles.MP02_AO6AI6)

    def test_write_allowed_coil_ignores_relay_mode_timeout_for_mr(self) -> None:
        close_transport = Mock()
        send = object()
        device = {"address": 1, "baudrate": 9600, "parity": "N", "stopbits": 1}
        module_kind = module_profiles.kind_from_type_code(module_profiles.MP02_DO4DI6)
        snapshot = {"ok": True}

        with (
            patch.object(device_config, "_open_transport", return_value=(send, close_transport)),
            patch.object(
                device_config,
                "_read_live_identity",
                return_value={
                    "signature": "MR-02m 4DO6DI",
                    "type_code": module_profiles.MP02_DO4DI6,
                },
            ),
            patch.object(device_config, "_kind_from_identity", return_value="mr"),
            patch.object(device_config, "_module_kind_from_identity", return_value=module_kind),
            patch.object(device_config, "_read_u16", return_value=1),
            patch.object(device_config, "write_single", return_value="timeout") as write_single_mock,
            patch.object(device_config, "write_coil", return_value=None) as write_coil_mock,
            patch.object(device_config, "snapshot_for_device", return_value=snapshot),
        ):
            result = device_config.write_allowed_coil("COM1", device, 1, True)

        self.assertEqual(result, snapshot)
        write_single_mock.assert_called_once_with(send, 1, device_config.REG_RELAY_MODE, 0, 1200)
        write_coil_mock.assert_called_once_with(send, 1, 1, True, 2000)
        close_transport.assert_called_once()

    def test_write_allowed_coil_accepts_bus_state_after_fc05_timeout(self) -> None:
        close_transport = Mock()
        send = object()
        device = {"address": 1, "baudrate": 9600, "parity": "N", "stopbits": 1}
        module_kind = module_profiles.kind_from_type_code(module_profiles.MP02_DO16)
        snapshot = {"ok": True}

        with (
            patch.object(device_config, "_open_transport", return_value=(send, close_transport)),
            patch.object(
                device_config,
                "_read_live_identity",
                return_value={
                    "signature": "MR-02m 16DO",
                    "type_code": module_profiles.MP02_DO16,
                },
            ),
            patch.object(device_config, "_kind_from_identity", return_value="mr"),
            patch.object(device_config, "_module_kind_from_identity", return_value=module_kind),
            patch.object(device_config, "write_coil", return_value="timeout") as write_coil_mock,
            patch.object(device_config, "_read_do_bits", return_value=[True] + [False] * 15),
            patch.object(device_config, "snapshot_for_device", return_value=snapshot),
        ):
            result = device_config.write_allowed_coil("COM1", device, 1, True)

        self.assertEqual(result, snapshot)
        write_coil_mock.assert_called_once_with(send, 1, 1, True, 2000)
        close_transport.assert_called_once()


class TestMrSnapshotPanel(unittest.TestCase):
    def test_panel_do_tab_fetches_full_do_block(self) -> None:
        send = object()
        slave = 3
        kind = module_profiles.kind_from_type_code(module_profiles.MP02_DO16)
        identity = {"signature": "MR-02m 16DO", "type_code": module_profiles.MP02_DO16}
        base_min = {
            "module": device_config._mr_module_payload(kind),
            "do": {"bits": [False] * 16, "counts": [0] * 16},
            "di": {"values": []},
            "ao": {"current_raw": [], "current_volts": []},
            "ai": {"channels": []},
            "inactivity_s": 5,
            "relay": {"mode": 0, "options": 0, "power_stagger": 0},
        }
        full_do = {
            "bits": [True] + [False] * 15,
            "counts": [2] * 16,
            "safe": [0] * 16,
            "timer_words": [0] * 6,
            "redelay": [0] * 6,
        }
        with (
            patch.object(device_config, "_read_mr_snapshot_minimal", return_value=base_min),
            patch.object(device_config, "_mr_do_block_full", return_value=full_do) as mock_do_full,
        ):
            out = device_config._read_mr_snapshot_panel(send, slave, identity, "do_2")
        mock_do_full.assert_called_once_with(send, slave, kind)
        self.assertEqual(out["do"], full_do)
        self.assertEqual(out["inactivity_s"], 5)


if __name__ == "__main__":
    unittest.main()
