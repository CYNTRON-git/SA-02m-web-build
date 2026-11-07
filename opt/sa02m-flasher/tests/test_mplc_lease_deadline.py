"""Deadline forwarding and independent manual-user bookkeeping."""
import threading
import time
import unittest
from unittest import mock

from sa02m_flasher import mplc_lease as adapter


class LeaseDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(adapter, name, {}) for name in
                        ("_MANUAL_LEASES", "_MANUAL_USERS", "_PENDING_RESTORES")]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.factory = mock.MagicMock()
        self.factory.return_value.__enter__.return_value.status = {"lease": {"services": []}}
        patch = mock.patch.object(adapter, "_lease_type", return_value=self.factory)
        patch.start()
        self.addCleanup(patch.stop)

    def manual(self):
        lease = mock.Mock(status={"lease": {"services": ["mplc4.service"]}})
        adapter._MANUAL_LEASES["/dev/COM4"] = lease
        return lease

    def test_budget_forwarding_and_unchanged_default_call(self):
        with adapter.port_lease("/dev/COM3", []):
            pass
        self.factory.assert_called_once_with("/dev/COM3", "flasher")
        self.factory.reset_mock()
        deadline = time.monotonic() + 1
        with adapter.port_lease("/dev/COM3", [], acquire_deadline=deadline, release_timeout=45):
            pass
        self.factory.assert_called_once_with("/dev/COM3", "flasher", acquire_deadline=deadline,
                                             release_timeout=45)

    def test_entry_lock_wait_uses_acquire_budget(self):
        held, release = threading.Event(), threading.Event()
        def hold():
            with adapter._LEASE_LOCK:
                held.set()
                release.wait(1)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(0.5))
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                with adapter.port_lease("/dev/COM4", [], acquire_deadline=started + 0.06):
                    self.fail("operation entered after its budget")
            self.assertLess(time.monotonic() - started, 0.20)
            self.factory.assert_not_called()
        finally:
            release.set()
            thread.join(1)

    def test_expired_and_nonfinite_acquire_budgets_never_enter_operation(self):
        for deadline, error in ((time.monotonic() - 1, TimeoutError),
                                (float("nan"), ValueError), (float("inf"), ValueError)):
            with self.subTest(deadline=deadline), self.assertRaises(error):
                with adapter.port_lease("/dev/COM4", [], acquire_deadline=deadline):
                    self.fail("invalid acquisition budget entered the operation")
        self.factory.assert_not_called()

    def test_entry_expiring_while_lock_is_acquired_releases_the_lock(self):
        with mock.patch.object(adapter.time, "monotonic", side_effect=[9.0, 11.0]):
            with self.assertRaises(TimeoutError):
                with adapter.port_lease("/dev/COM4", [], acquire_deadline=10.0):
                    self.fail("expired lock acquisition entered the operation")
        available = []
        def acquire():
            acquired = adapter._LEASE_LOCK.acquire(timeout=0.1)
            available.append(acquired)
            if acquired:
                adapter._LEASE_LOCK.release()
        thread = threading.Thread(target=acquire)
        thread.start()
        thread.join(0.3)
        self.assertEqual(available, [True])

    def test_active_manual_port_does_not_serialize_another_ports_operation(self):
        manual = self.manual()
        entered, finish = threading.Event(), threading.Event()
        def use_manual():
            with adapter.port_lease("/dev/COM4", [], acquire_deadline=time.monotonic() + 0.3):
                entered.set()
                finish.wait(1)
        thread = threading.Thread(target=use_manual)
        thread.start()
        try:
            self.assertTrue(entered.wait(0.5))
            with adapter.port_lease("/dev/COM3", [], acquire_deadline=time.monotonic() + 0.1):
                pass
            with self.assertRaises(adapter.PortBusyError):
                with adapter.port_lease("/dev/COM4", [], acquire_deadline=time.monotonic() + 0.1):
                    self.fail("two operations entered the same manual reserve")
            with mock.patch.object(adapter, "_coordinator_snapshot", return_value=(None, False)):
                result = adapter.restore_pollers([])
            self.assertIn("/dev/COM4: operation active", result["failed"])
            manual.close.assert_not_called()
        finally:
            finish.set()
            thread.join(1)
        self.assertEqual(adapter._MANUAL_USERS["/dev/COM4"], 0)

    def test_manual_exit_does_not_wait_for_unrelated_service_rpc_lock(self):
        self.manual()
        entered, finish, exited = threading.Event(), threading.Event(), threading.Event()
        def use_manual():
            with adapter.port_lease("/dev/COM4", []):
                entered.set()
                finish.wait(1)
            exited.set()
        thread = threading.Thread(target=use_manual)
        thread.start()
        self.assertTrue(entered.wait(0.5))
        try:
            with adapter._LEASE_LOCK:
                finish.set()
                closed_during_rpc = exited.wait(0.2)
        finally:
            finish.set()
            thread.join(1)
        self.assertTrue(closed_during_rpc, "manual user cleanup waited for an unrelated RPC")
        self.assertEqual(adapter._MANUAL_USERS["/dev/COM4"], 0)

    def test_restore_failure_is_not_reported_as_operation_success(self):
        self.factory.return_value.__exit__.side_effect = RuntimeError("Coordinator recovery pending")
        with self.assertRaisesRegex(RuntimeError, "recovery pending"):
            with adapter.port_lease("/dev/COM3", [], release_timeout=0.1):
                pass
        with mock.patch.object(adapter, "_coordinator_snapshot", return_value=(None, False)):
            self.assertTrue(adapter.restoration_status("/dev/COM3")["restore_pending"])

    def test_body_or_admission_error_does_not_create_a_recovery_obligation(self):
        with self.assertRaisesRegex(ValueError, "bad response"):
            with adapter.port_lease("/dev/COM3", []):
                raise ValueError("bad response")
        self.factory.return_value.__enter__.side_effect = RuntimeError("UART already leased")
        with self.assertRaisesRegex(RuntimeError, "already leased"):
            with adapter.port_lease("/dev/COM3", []):
                self.fail("operation entered a busy UART")
        self.assertEqual(adapter._PENDING_RESTORES, {})


if __name__ == "__main__":
    unittest.main()
