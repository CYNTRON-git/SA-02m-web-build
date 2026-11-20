"""The engine must execute the exact approved document revision under locks."""
import copy
from contextlib import ExitStack
import functools
from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "sa02m-rules")]
from sa02m_alice.home.adapter import BoardAdapter
from sa02m_alice.common import config_store
from sa02m_rules.document_lock import command_lock
from sa02m_rules.home_command import _verified_run


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog_dir = Path(self.tmp.name) / "catalog"
        self.rules_dir = Path(self.tmp.name) / "rules"
        self.catalog_dir.mkdir()
        self.rules_dir.mkdir()
        self.catalog = {"devices": [{"id": "lamp", "name": "Lamp"}], "rooms": []}
        self.rules = {"scenarios": [{"id": "scene1", "name": "Approved scene", "action": [{"value": 0}]}]}
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config_store.C, "DEVICES_CONF", str(self.catalog_dir / "devices.json")))
        self.stack.enter_context(patch("sa02m_alice.home.adapter.load_devices", side_effect=lambda: copy.deepcopy(self.catalog)))
        store = SimpleNamespace(load=lambda: copy.deepcopy(self.rules), command_lock=functools.partial(command_lock, str(self.rules_dir / "scenarios.json")))
        self.stack.enter_context(patch("sa02m_alice.home.adapter.config._rules_store", return_value=store))
        self.claim = {"scenario_id": "scene1", "deadline": 105, "expected_revision": BoardAdapter(None).revision()}
        self.runs = []
        self.engine = SimpleNamespace(run_now=lambda sid: self.runs.append(sid) or {"ok": True})

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_edit_between_prepare_and_delivery_runs_nothing(self):
        self.rules["scenarios"][0]["action"][0]["value"] = 1
        self.assertIsNone(_verified_run(self.claim, self.engine, lambda: self.fail("reload must not happen"), lambda: 100))
        self.assertFalse(self.runs)

    def test_catalog_edit_also_invalidates_prepared_scene(self):
        self.catalog["devices"][0]["name"] = "Changed mapping"
        self.assertIsNone(_verified_run(self.claim, self.engine, lambda: None, lambda: 100))
        self.assertFalse(self.runs)

    def test_deadline_after_reload_prevents_execute(self):
        now = [100]
        def reload():
            now[0] = 106
        self.assertIsNone(_verified_run(self.claim, self.engine, reload, lambda: now[0]))
        self.assertFalse(self.runs)

    @unittest.skipUnless(os.name == "posix", "POSIX directory locks")
    def test_both_real_directory_locks_are_held_through_run(self):
        import fcntl
        def assert_locked():
            for directory in (self.catalog_dir, self.rules_dir):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
        def run(sid):
            assert_locked()
            self.runs.append(sid)
            return {"ok": True}
        self.engine.run_now = run
        _verified_run(self.claim, self.engine, assert_locked, lambda: 100)
        self.assertEqual(self.runs, ["scene1"])


if __name__ == "__main__":
    unittest.main()
