"""identity.py — machine-id binding (clone safety layer 1) and pairing-store sanity."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from sa02m_homekit import constants as C
from sa02m_homekit import identity

GOOD_STATE = {
    "mac": "AA:BB:CC:DD:EE:FF", "config_version": 2, "paired_clients": {},
    "private_key": "ab" * 32, "public_key": "cd" * 32,
    "pincode": "123-45-679", "setup_id": "AB12",
}


class MachineBindingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.var = os.path.join(self.dir, "var")
        os.makedirs(self.var)
        self.mid = os.path.join(self.dir, "machine-id")
        self._write_mid("aaaa")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_mid(self, text):
        with open(self.mid, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def _state(self):
        path = os.path.join(self.var, "state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(GOOD_STATE, fh)
        with open(os.path.join(self.var, ".hk-abc.tmp"), "w") as fh:
            fh.write("torn")
        with open(os.path.join(self.var, "aids.json"), "w") as fh:
            fh.write("{}")
        return path

    def test_first_start_records_binding_and_keeps_nothing_to_wipe(self):
        self.assertIsNone(identity.check_machine_binding(self.var, self.mid))
        with open(os.path.join(self.var, "identity.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec, {"machine_id_sha256": hashlib.sha256(b"aaaa").hexdigest()})

    def test_match_keeps_the_pairing_store(self):
        identity.check_machine_binding(self.var, self.mid)
        state = self._state()
        self.assertIsNone(identity.check_machine_binding(self.var, self.mid))
        self.assertTrue(os.path.exists(state))

    def test_mismatch_deletes_state_and_sidecars_keeps_aids(self):
        identity.check_machine_binding(self.var, self.mid)
        state = self._state()
        self._write_mid("bbbb")  # a cloned image booted on another board
        with self.assertLogs("sa02m_homekit.identity", "WARNING") as logs:
            reason = identity.check_machine_binding(self.var, self.mid)
        self.assertEqual(reason, C.REASON_IDENTITY_REGENERATED)
        self.assertIn("HomeKit identity regenerated (machine-id changed)", "\n".join(logs.output))
        self.assertFalse(os.path.exists(state))
        self.assertEqual(sorted(os.listdir(self.var)), ["aids.json", "identity.json"])
        # the new board is now the bound one
        self.assertIsNone(identity.check_machine_binding(self.var, self.mid))

    def test_state_without_binding_is_treated_as_foreign(self):
        state = self._state()
        with self.assertLogs("sa02m_homekit.identity", "WARNING"):
            reason = identity.check_machine_binding(self.var, self.mid)
        self.assertEqual(reason, C.REASON_IDENTITY_REGENERATED)
        self.assertFalse(os.path.exists(state))

    def test_no_machine_id_enforces_nothing(self):
        state = self._state()
        os.unlink(self.mid)
        with self.assertLogs("sa02m_homekit.identity", "WARNING"):
            self.assertIsNone(identity.check_machine_binding(self.var, self.mid))
        self.assertTrue(os.path.exists(state))


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, body):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_absent_is_fine(self):
        self.assertIsNone(identity.check_state_file(self.path))

    def test_good_is_kept(self):
        self._write(json.dumps(GOOD_STATE))
        self.assertIsNone(identity.check_state_file(self.path))
        self.assertTrue(os.path.exists(self.path))

    def test_torn_json_is_regenerated(self):
        self._write('{"mac": "AA')
        with self.assertLogs("sa02m_homekit.identity", "ERROR"):
            self.assertEqual(identity.check_state_file(self.path), C.REASON_STATE_CORRUPT)
        self.assertFalse(os.path.exists(self.path))

    def test_partial_or_forbidden_code_is_regenerated(self):
        for broken in ({k: v for k, v in GOOD_STATE.items() if k != "private_key"},
                       dict(GOOD_STATE, pincode="111-11-111"),
                       dict(GOOD_STATE, setup_id="ab"),
                       dict(GOOD_STATE, private_key="00")):
            self._write(json.dumps(broken))
            with self.assertLogs("sa02m_homekit.identity", "ERROR"):
                reason = identity.check_state_file(self.path)
            self.assertEqual(reason, C.REASON_STATE_CORRUPT, broken)
            self.assertFalse(os.path.exists(self.path))


class SetupCodeRuleTests(unittest.TestCase):
    def test_forbidden_codes(self):
        self.assertEqual(len(C.FORBIDDEN_SETUP_CODES), 12)
        for code in ("000-00-000", "999-99-999", "123-45-678", "876-54-321"):
            self.assertFalse(identity.valid_setup_code(code), code)
        self.assertTrue(identity.valid_setup_code("031-45-154"))
        self.assertFalse(identity.valid_setup_code("03145154"))


if __name__ == "__main__":
    unittest.main()
