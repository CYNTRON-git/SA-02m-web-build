"""Exercise harness validates restoration; it never accepts a missing owner."""

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("exercise", ROOT / "exercise.py")
exercise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exercise)


class ExerciseTests(unittest.TestCase):
    def state(self, active="active", group="mqtt.service", leases=None):
        return {"units": {"mqtt.service": {"ActiveState": active, "UnitFileState": "disabled"}},
                "owners": {"/dev/COM4": [{"pid": 42, "cgroup": group}]},
                "leases": leases or [], "services": {}}

    def test_read_request_crc(self):
        self.assertEqual(exercise.crc(bytes.fromhex("010400000001")), 0xca31)

    def test_restored_process_can_have_new_pid(self):
        base, current = self.state(), self.state()
        current["owners"]["/dev/COM4"][0]["pid"] = 999
        with patch.object(exercise, "snapshot", return_value=current):
            self.assertEqual(exercise.wait_restored(["/dev/COM4"], ["mqtt.service"], base), current)

    def test_changed_service_owner_or_pending_lease_is_failure(self):
        for current in (self.state(active="inactive"), self.state(group="unknown.service"),
                        self.state(leases=[{"port": "/dev/COM4"}])):
            with self.subTest(current=current), patch.object(exercise, "snapshot", return_value=current):
                with self.assertRaisesRegex(RuntimeError, "not restored"):
                    exercise.wait_restored(["/dev/COM4"], ["mqtt.service"], self.state(), timeout=0)


if __name__ == "__main__":
    unittest.main()
