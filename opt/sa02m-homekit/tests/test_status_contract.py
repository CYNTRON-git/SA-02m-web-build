"""status.py — the three /run files: keys, modes, setup.json lifetime, no code leak."""

from __future__ import annotations

import grp
import json
import os
import shutil
import stat
import tempfile
import unittest

from sa02m_homekit import constants as C
from sa02m_homekit import status as S

CODE = "031-45-154"


class StatusWriterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.w = S.StatusWriter(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _status(self):
        with open(self.w.status_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _setup(self):
        self.w.write_setup(CODE, "X-HM://0000000007OSX", "7OSX", ["1" * 21] * 21)

    def test_status_keys_are_exactly_the_contract_set(self):
        self.w.write_status(C.STATE_RUNNING, paired=False)
        self.assertEqual(set(self._status()), set(S.STATUS_KEYS))

    def test_status_is_world_readable_0644(self):
        self.w.write_status(C.STATE_DISABLED)
        self.assertEqual(stat.S_IMODE(os.stat(self.w.status_path).st_mode), 0o644)

    def test_setup_and_projection_are_0640_group_www_data(self):
        self._setup()
        self.w.write_projection({"accessories": [], "skipped": [], "skipped_total": 0})
        for path in (self.w.setup_path, self.w.projection_path):
            st = os.stat(path)
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o640, path)
            try:
                gid = grp.getgrnam(C.WEB_GROUP).gr_gid
            except KeyError:
                self.skipTest("SKIPPED, NOT PASSED: no %s group on this host" % C.WEB_GROUP)
            self.assertEqual(st.st_gid, gid, path)

    def test_setup_survives_only_running_and_unpaired(self):
        self._setup()
        self.w.write_status(C.STATE_RUNNING, paired=False)
        self.assertTrue(os.path.exists(self.w.setup_path))
        self.w.write_status(C.STATE_RUNNING, paired=True)
        self.assertFalse(os.path.exists(self.w.setup_path))
        for state in (C.STATE_STARTING, C.STATE_DISABLED, C.STATE_MISSING_DEPS,
                      C.STATE_NO_INTERFACE, C.STATE_PORT_IN_USE, C.STATE_ERROR):
            self._setup()
            self.w.write_status(state, paired=False)
            self.assertFalse(os.path.exists(self.w.setup_path), state)
        self._setup()
        self.w.write_status(C.STATE_RUNNING, paired=None)
        self.assertFalse(os.path.exists(self.w.setup_path))

    def test_code_never_in_status(self):
        self._setup()
        self.w.write_status(C.STATE_RUNNING, paired=False, message="ok", bridge_name="SA-02m ABCDEF")
        with open(self.w.status_path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertNotIn(CODE, body)
        self.assertNotIn(CODE.replace("-", ""), body)
        self.assertNotIn("X-HM://", body)

    def test_not_installed_is_never_written_by_the_daemon(self):
        with self.assertRaises(ValueError):
            self.w.write_status(C.STATE_NOT_INSTALLED)
        with self.assertRaises(ValueError):
            self.w.write_status("connected")

    def test_states_and_reasons_are_distinct_enums(self):
        self.assertEqual(len(set(C.STATES)), len(C.STATES))
        self.assertEqual(set(C.STATES), {
            "not_installed", "disabled", "starting", "running", "missing_deps",
            "no_interface", "port_in_use", "error"})
        self.assertEqual(set(C.REASONS), {
            "identity_regenerated", "state_corrupt_regenerated", "pair_setup_locked", "status_stale"})
        self.assertTrue(C.HEARTBEAT_STATES <= set(C.STATES))


if __name__ == "__main__":
    unittest.main()
