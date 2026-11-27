"""Canonical request identity is closed, bounded and never stores generation."""

import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

from sa02m_devices import event_journal_request as request
from sa02m_devices.event_journal_state import JournalStateError


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.value = {"version": 1, "identity": "a" * 32, "fingerprint": "b" * 64,
                      "settings": dict(zip(request.SETTINGS, (str(root / 'state space $x\\literal'),
                          str(root / 'receipt'), str(root / 'update'), '1'))),
                      "context": {"mode": "absent"},
                      "locks": {"install": {"device": 1, "inode": 2, "mode": 0o600},
                                "update": {"device": 1, "inode": 3, "mode": 0o644}}}

    def test_roundtrip_and_stable_name_are_lossless(self):
        wire = request.encode(self.value)
        self.assertEqual(request.decode(wire), self.value)
        reversed_value = dict(reversed(list(self.value.items())))
        self.assertEqual(request.encode(reversed_value), wire)
        self.assertEqual(request.unit_name(wire), request.unit_name(request.encode(reversed_value)))
        self.assertLessEqual(len(wire.encode()), 4096)
        self.assertRegex(request.unit_name(wire), r'^sa02m-journal-complete-[0-9a-f]{64}\.service$')

    def test_context_source_and_identity_changes_get_distinct_names(self):
        original = request.unit_name(request.encode(self.value))
        for key, value in (("identity", "c" * 32), ("fingerprint", "d" * 64),
                           ("context", {"mode": "pending", "id": "12345678-1234-1234-1234-123456789abc",
                            "operation": "update", "source": "file", "target_version": "1.0.8.2", "target_commit": "a" * 40})):
            with self.subTest(key=key):
                self.assertNotEqual(original, request.unit_name(request.encode({**self.value, key: value})))

    def test_unknown_missing_duplicate_and_noncanonical_fields_refuse(self):
        for key in request.KEYS:
            bad = dict(self.value)
            del bad[key]
            with self.subTest(missing=key), self.assertRaises(JournalStateError):
                request.encode(bad)
        with self.assertRaises(JournalStateError):
            request.encode({**self.value, "generation": "legacy"})
        for raw in ('{"version":1,"version":1}', json.dumps(self.value, indent=2), '[]'):
            with self.subTest(raw=raw[:20]), self.assertRaises(JournalStateError):
                request.decode(base64.urlsafe_b64encode(raw.encode()).decode())
        for wire in ('?', 'e30= ', 'A' * 4097):
            with self.subTest(wire=wire[:20]), self.assertRaises(JournalStateError):
                request.decode(wire)

    def test_wrong_types_and_unsafe_configurations_refuse(self):
        for key, value in (("version", True), ("identity", "legacy"), ("identity", None),
                           ("fingerprint", "B" * 64), ("context", {"mode": "unknown"}),
                           ("locks", {"install": {"device": 1, "inode": True, "mode": 0o600}})):
            with self.subTest(key=key, value=value), self.assertRaises(JournalStateError):
                request.encode({**self.value, key: value})
        for name, value in ((request.SETTINGS[0], 'relative'), (request.SETTINGS[-1], '2'),
                            (request.SETTINGS[1], self.value['settings'][request.SETTINGS[0]])):
            bad = copy.deepcopy(self.value)
            bad['settings'][name] = value
            with self.subTest(name=name), self.assertRaises(JournalStateError):
                request.encode(bad)


if __name__ == '__main__':
    unittest.main()
