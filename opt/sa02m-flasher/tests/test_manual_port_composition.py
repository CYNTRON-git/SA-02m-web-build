"""Real broker/SCM_RIGHTS/PTYS and MQTT pause through the manual flasher API."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

if os.name == "nt":
    raise unittest.SkipTest("Unix descriptor/PTY composition requires Linux")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sa02m-port-coordinator/tests"))
import test_process_ownership as ownership
from sa02m_ports import Lease
from sa02m_ports.broker import occupants
from sa02m_flasher import mplc_lease


class ManualPortComposition(unittest.TestCase):
    def test_failed_restore_stays_pending_until_broker_recovers(self):
        h = ownership.ProcessOwnership()
        h.setUp()
        self.addCleanup(h.tearDown)
        mapping = dict(zip(("/dev/COM1", "/dev/COM2"), h.ports))
        with patch.object(mplc_lease, "_MANUAL_LEASES", {}), \
                patch.object(mplc_lease, "_MANUAL_USERS", {}), \
                patch.object(mplc_lease, "_PENDING_RESTORES", {}, create=True), \
                patch.object(mplc_lease, "COORDINATOR_STATUS", Path(h.root, "run/status.json")), \
                patch.object(mplc_lease.Path, "exists", lambda p: str(p) in mapping), \
                patch.object(mplc_lease, "_lease_type", return_value=lambda port, purpose: Lease(mapping[port], purpose, h.path)):
            self.assertFalse(mplc_lease.release_pollers([])["failed"])
            data = h.system()
            data["fail"] = True
            ownership.atomic_json(h.system_file, data)
            first = mplc_lease.restore_pollers([])
            self.assertTrue(first["failed"])
            self.assertFalse(mplc_lease._MANUAL_LEASES)
            self.assertFalse(h.system()["active"])
            self.assertEqual(h.state()["services"]["mplc4.service"]["phase"], "restore_failed")
            second = mplc_lease.restore_pollers([])
            self.assertTrue(second["failed"], "a no-op close must not confirm service recovery")
            self.assertFalse(h.system()["active"])
            # A fresh browser, or a restarted flasher, reconstructs both ports
            # affected by a whole-service obligation from the broker snapshot.
            mplc_lease._PENDING_RESTORES.clear()
            for port in h.ports:
                info = mplc_lease.restoration_status(port)
                self.assertTrue(info["restore_pending"])
                self.assertEqual(info["restore_services"], ["mplc4.service"])
                self.assertTrue(info["coordinator_available"])
            # A plausible empty file from a different broker process cannot
            # clear obligations belonging to the recorded live process.
            status = Path(h.root, "run/status.json")
            stale = ownership.json.loads(status.read_text())
            stale.update(leases=[], services={}, broker_start_ticks="0")
            probe = status.with_name("probe.json")
            ownership.atomic_json(probe, stale)
            with patch.object(mplc_lease, "COORDINATOR_STATUS", probe):
                self.assertTrue(mplc_lease.restore_pollers([])["failed"])
                self.assertFalse(mplc_lease.restoration_status(h.ports[0])["coordinator_available"])
            with patch.object(mplc_lease, "COORDINATOR_STATUS", Path(h.root, "absent.json")):
                self.assertTrue(mplc_lease.restore_pollers([])["failed"])
                self.assertTrue(mplc_lease.restoration_status(h.ports[0])["restore_pending"])
                self.assertFalse(mplc_lease.restoration_status(h.ports[0])["coordinator_available"])
            data["fail"] = False
            ownership.atomic_json(h.system_file, data)
            h.wait(lambda: not h.state()["services"])
            self.assertTrue(h.system()["active"])
            self.assertFalse(mplc_lease.restore_pollers([])["failed"])
            for port in h.ports:
                self.assertFalse(mplc_lease.restoration_status(port)["restore_pending"])
            self.assertEqual(h.system()["starts"], 1)

    def test_partial_reserve_preserves_gateway_and_restore_resumes_mqtt(self):
        import pty
        h = ownership.ProcessOwnership()
        h.setUp()
        self.addCleanup(h.tearDown)
        h.broker.kill()
        h.broker.join(3)
        master, slave = pty.openpty()
        h.masters.append(master)
        h.ports.append(os.ttyname(slave))
        os.close(slave)
        h.start_broker()
        parent, child = ownership.CTX.Pipe()
        mqtt = ownership.CTX.Process(target=ownership.run_mqtt, args=(h.root, h.ports[:2], child))
        mqtt.start()
        h.children.append(mqtt)
        self.assertTrue(parent.poll(10))
        self.assertTrue(parent.recv())
        mapping = dict(zip(("/dev/COM1", "/dev/COM2", "/dev/COM4"), h.ports))
        gateway = Lease(h.ports[2], "gateway", h.path)
        gateway.__enter__()
        self.addCleanup(gateway.close)
        fd = os.open(h.ports[2], os.O_RDWR | os.O_NOCTTY)
        self.addCleanup(os.close, fd)
        gateway_fd = gateway.fd
        with patch.object(mplc_lease, "_MANUAL_LEASES", {}), \
                patch.object(mplc_lease, "_MANUAL_USERS", {}), \
                patch.object(mplc_lease.Path, "exists", lambda p: str(p) in mapping), \
                patch.object(mplc_lease, "_lease_type", return_value=lambda port, purpose: Lease(mapping[port], purpose, h.path)):
            try:
                result = mplc_lease.release_pollers([])
                self.assertEqual(result["manual_reserved_ports"], ["/dev/COM1", "/dev/COM2"])
                self.assertEqual(len(result["failed"]), 1)
                self.assertIn("/dev/COM4:", result["failed"][0])
                self.assertEqual(gateway.fd, gateway_fd)
                os.fstat(gateway_fd)
                self.assertIn(os.getpid(), occupants(h.ports[2]))
                for port in h.ports[:2]:
                    self.assertNotIn(mqtt.pid, occupants(port))
                restored = mplc_lease.restore_pollers([])
                self.assertFalse(restored["failed"])
                for port in h.ports[:2]:
                    h.wait(lambda: mqtt.pid in occupants(port))
                self.assertEqual(len(h.state()["leases"]), 1)
                self.assertEqual(next(iter(h.state()["leases"].values()))["purpose"], "gateway")
            finally:
                mplc_lease.restore_pollers([])
