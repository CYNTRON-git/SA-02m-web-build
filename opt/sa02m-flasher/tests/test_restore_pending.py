"""Only authoritative current broker evidence can clear a recovery obligation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sa02m_flasher import mplc_lease as lease


class PendingRestoration(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(lease, "_PENDING_RESTORES", {}).start()
        self.read = patch.object(lease, "_coordinator_snapshot").start()
        self.clean = {"timestamp": 30, "leases": [], "services": {},
                      "configured_scopes": {"/dev/COM1": ["mplc4.service"], "/dev/COM2": ["mplc4.service"]}}
        lease._remember_restore("/dev/COM1", ["mplc4.service"], "closed", "start failed", 20)

    def test_unavailable_or_older_empty_snapshot_cannot_clear_known_pending(self):
        for snapshot, available in [(None, False), (self.clean, False), ({**self.clean, "timestamp": 19}, True)]:
            with self.subTest(snapshot=snapshot, available=available):
                self.read.return_value = snapshot, available
                self.assertTrue(lease.restoration_status("/dev/COM1")["restore_pending"])
        self.read.return_value = self.clean, True
        self.assertFalse(lease.restoration_status("/dev/COM1")["restore_pending"])

    def test_service_shared_with_another_lease_remains_pending(self):
        snapshot = copy.deepcopy(self.clean)
        snapshot["leases"] = [{"id": "other", "port": "/dev/COM2", "services": ["mplc4.service"], "phase": "owned"}]
        snapshot["services"] = {"mplc4.service": {"phase": "paused", "error": ""}}
        self.read.return_value = snapshot, True
        self.assertTrue(lease.restoration_status("/dev/COM1")["restore_pending"])
        snapshot["leases"] = []
        self.assertTrue(lease.restoration_status("/dev/COM1")["restore_pending"])
        snapshot["services"] = {}
        self.assertFalse(lease.restoration_status("/dev/COM1")["restore_pending"])

    def test_recovering_uart_without_whole_service_is_visible_after_restart(self):
        lease._PENDING_RESTORES.clear()
        snapshot = copy.deepcopy(self.clean)
        snapshot["leases"] = [{"id": "held", "port": "/dev/COM1", "services": [], "phase": "recovering", "error": ""}]
        self.read.return_value = snapshot, True
        self.assertTrue(lease.restoration_status("/dev/COM1")["restore_pending"])
        self.assertEqual(lease.restoration_status("/dev/COM1")["restore_services"], [])
        snapshot["leases"] = []
        self.assertFalse(lease.restoration_status("/dev/COM1")["restore_pending"])


class SnapshotValidation(unittest.TestCase):
    def test_malformed_and_oversize_snapshots_are_not_completion_evidence(self):
        valid = {"timestamp": 1, "leases": [], "services": {}, "configured_scopes": {}}
        cases = ["not json", "x" * 131073]
        cases += [json.dumps({**valid, **change}) for change in (
            {"timestamp": None}, {"timestamp": float("nan")}, {"leases": {}},
            {"services": []}, {"configured_scopes": []},
            {"leases": [{"id": "x", "port": "/dev/COM1", "services": [1]}]},
            {"services": {"mplc4.service": {}}}, {"configured_scopes": {"/dev/COM1": [1]}},
        )]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "status.json")
            with patch.object(lease, "COORDINATOR_STATUS", path):
                for content in cases:
                    with self.subTest(content=content[:100]):
                        path.write_text(content, encoding="utf-8")
                        self.assertEqual(lease._coordinator_snapshot(), (None, False))
                for pid in (0, -1, "bad"):
                    path.write_text(json.dumps({**valid, "broker_pid": pid}), encoding="utf-8")
                    self.assertFalse(lease._coordinator_snapshot()[1])


if __name__ == "__main__":
    unittest.main()
