# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from sa02m_flasher import mplc_lease


class TestMplcLease(unittest.TestCase):
    def test_stop_service_uses_process_path_for_mplc(self) -> None:
        with (
            patch.object(mplc_lease, "resolve_service_name", return_value="mplc.service"),
            patch.object(mplc_lease, "_pkill_names", return_value=True) as pkill_mock,
            patch.object(mplc_lease, "_proc_is_running", return_value=False) as proc_mock,
            patch.object(mplc_lease, "_run") as run_mock,
        ):
            ok = mplc_lease.stop_service("mplc.service")

        self.assertTrue(ok)
        pkill_mock.assert_called_once_with(["mplc", "mplc4", "mplc_monitor"])
        self.assertGreaterEqual(proc_mock.call_count, 1)
        run_mock.assert_not_called()

    def test_is_port_poll_free_when_fuser_empty(self) -> None:
        with (
            patch.object(mplc_lease, "device_path_exists", return_value=True),
            patch.object(mplc_lease, "port_occupants", return_value=[]),
        ):
            self.assertTrue(mplc_lease.is_port_poll_free("/dev/COM4"))

    def test_is_port_poll_free_false_when_occupied(self) -> None:
        with (
            patch.object(mplc_lease, "device_path_exists", return_value=True),
            patch.object(mplc_lease, "port_occupants", return_value=["1234"]),
        ):
            self.assertFalse(mplc_lease.is_port_poll_free("/dev/COM4"))

    def test_port_lease_quick_path_skips_stop(self) -> None:
        with (
            patch.object(mplc_lease, "is_port_poll_free", return_value=True),
            patch.object(mplc_lease, "port_occupants", return_value=[]),
            patch.object(mplc_lease, "is_service_active") as active_mock,
            patch.object(mplc_lease, "stop_service") as stop_mock,
            patch.object(mplc_lease, "start_service") as start_mock,
        ):
            with mplc_lease.port_lease("/dev/COM4", ["mplc.service", "sa02m-modbus-mqtt.service"]):
                pass
        active_mock.assert_not_called()
        stop_mock.assert_not_called()
        start_mock.assert_not_called()

    def test_port_lease_slow_path_stops_active(self) -> None:
        with (
            patch.object(mplc_lease, "is_port_poll_free", return_value=False),
            patch.object(mplc_lease, "port_occupants", return_value=[]),
            patch.object(mplc_lease, "resolve_service_name", return_value="mplc4.service"),
            patch.object(mplc_lease, "is_service_active", return_value=True),
            patch.object(mplc_lease, "stop_service", return_value=True) as stop_mock,
            patch.object(mplc_lease, "start_service", return_value=True) as start_mock,
        ):
            with mplc_lease.port_lease("/dev/COM4", ["mplc.service"]) as stopped:
                self.assertEqual(stopped, ["mplc4.service"])
        stop_mock.assert_called_once_with("mplc4.service")
        start_mock.assert_called_once_with("mplc4.service")


if __name__ == "__main__":
    unittest.main()
