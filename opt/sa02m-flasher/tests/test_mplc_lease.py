# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

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

    def test_empty_fuser_still_requires_broker(self) -> None:
        # The old quick path skipped exclusion; the new contract never does.
        factory = MagicMock()
        context = factory.return_value
        context.__enter__.return_value.status = {"lease": {"services": []}}
        with patch.object(mplc_lease, "_lease_type", return_value=factory), \
                patch.object(mplc_lease, "is_port_poll_free", return_value=True), \
                patch.object(mplc_lease, "stop_service") as stop:
            with mplc_lease.port_lease("/dev/COM4", ["mplc.service"]):
                pass
        factory.assert_called_once_with("/dev/COM4", "flasher")
        context.__exit__.assert_called_once()
        stop.assert_not_called()

    def test_broker_scope_released_on_operation_exception(self) -> None:
        factory = MagicMock()
        context = factory.return_value
        context.__enter__.return_value.status = {"lease": {"services": ["mplc4.service"]}}
        with patch.object(mplc_lease, "_lease_type", return_value=factory):
            with self.assertRaisesRegex(ValueError, "operation failed"):
                with mplc_lease.port_lease("/dev/COM4", ["arbitrary.service"]) as scope:
                    self.assertEqual(scope, ["mplc4.service"])
                    raise ValueError("operation failed")
        context.__exit__.assert_called_once()
        self.assertIs(context.__exit__.call_args.args[0], ValueError)

    def test_missing_broker_fails_closed(self) -> None:
        factory = MagicMock()
        factory.return_value.__enter__.side_effect = RuntimeError("unavailable")
        with patch.object(mplc_lease, "_lease_type", return_value=factory):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                with mplc_lease.port_lease("/dev/COM4", []):
                    self.fail("operation ran without coordinator")


if __name__ == "__main__":
    unittest.main()
