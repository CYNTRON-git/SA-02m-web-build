"""Manual all-port reservations retain partial results and remain recoverable."""
import unittest
from unittest.mock import MagicMock, patch

from sa02m_flasher import mplc_lease as lease


class ManualReservations(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(lease, "_MANUAL_LEASES", {}).start()
        patch.object(lease, "_MANUAL_USERS", {}).start()
        patch.object(lease, "_PENDING_RESTORES", {}).start()
        patch.object(lease.Path, "exists", return_value=True).start()
        self.instances = {}

        def factory(port, purpose):
            if port == "/dev/COM4":
                raise RuntimeError("UART already leased")
            result = MagicMock(status={"lease": {"services": []}})
            self.instances[port] = result
            return result

        patch.object(lease, "_lease_type", return_value=factory).start()

    def test_partial_names_refusal_and_all_retained_reservations(self):
        result = lease.release_pollers([])
        retained = ["/dev/COM1", "/dev/COM2", "/dev/COM3", "/dev/COM5"]
        self.assertEqual(result["reserved_ports"], retained)
        self.assertEqual(result["failed"], ["/dev/COM4: UART already leased"])
        self.assertEqual(result["manual_reserved_ports"], retained)
        again = lease.release_pollers([])
        self.assertEqual(again["reserved_ports"], [])
        self.assertEqual(again["manual_reserved_ports"], retained)
        self.assertEqual(lease.restore_pollers([])["restarted"], retained)
        self.assertFalse(lease.restore_pollers([])["failed"])

    def test_all_empty_scopes_succeed_and_repeat_is_idempotent(self):
        with patch.object(lease, "_lease_type", return_value=lambda *args: MagicMock(status={"lease": {"services": []}})):
            result = lease.release_pollers(["unlisted.service"])
            self.assertFalse(result["failed"])
            self.assertEqual(result["stopped_now"], [])
            self.assertEqual(result["reserved_ports"], ["/dev/COM%d" % n for n in range(1, 6)])
            again = lease.release_pollers([])
            self.assertEqual(again["reserved_ports"], [])
            self.assertEqual(again["manual_reserved_ports"], result["reserved_ports"])

    def test_active_user_stays_reserved_and_close_failure_is_visible(self):
        lease.release_pollers([])
        lease._MANUAL_USERS["/dev/COM1"] = 1
        self.instances["/dev/COM2"].close.side_effect = RuntimeError("restore failed")
        result = lease.restore_pollers([])
        self.assertEqual(len(result["failed"]), 2)
        self.assertTrue(lease.is_reserved("/dev/COM1"))
        self.assertFalse(lease.is_reserved("/dev/COM2"))
        self.instances["/dev/COM1"].close.assert_not_called()

    def test_known_owner_labels_are_diagnostic_and_specific(self):
        for command, expected in [
            ("/usr/bin/python3 /opt/sa02m-serial-gateway/serial_gateway.py", "RS-485 Gateway"),
            ("/usr/bin/python3 -u /opt/sa02m-serial-gateway/serial_gateway.py", "RS-485 Gateway"),
            ("/usr/bin/echo /opt/sa02m-serial-gateway/serial_gateway.py", ""),
            ("  ", ""),
            ("/usr/bin/vplc --config /etc/vplc.yaml", "vPLC"),
            ("vplc.service", "vPLC"),
            ("sa02m-vplc.service", "vPLC"),
            ("/usr/bin/python3 /tmp/not_a_serial_gateway.py", ""),
            ("/usr/bin/python3 /tmp/fake.py --label vplc", ""),
            ("modbus_mqtt_bridge.py", "MQTT"),
            ("mplc4", "MPLC4"),
            ("sa02m-flasher", "Flasher"),
        ]:
            with self.subTest(command=command):
                self.assertEqual(lease._poll_label_from_cmdline(command), expected)


if __name__ == "__main__":
    unittest.main()
