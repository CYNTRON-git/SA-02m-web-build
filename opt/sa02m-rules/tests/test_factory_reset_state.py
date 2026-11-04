"""The store as a factory reset leaves it (contract cloud-scenarios.md
«Сброс к заводским настройкам»): scenarios.json kept, runs.json replaced by the
empty journal, logic_state.json removed.

Characterization, GREEN by design: it pins the store behaviour the reset
runner (etc/sa02m-factory-reset-runner.sh) relies on, so a store change that
breaks it fails here — the runner itself is exercised end to end by
scripts/dev/test-factory-reset-runner.py (R-cases). Why the journal is written
EMPTY rather than deleted is case (b): with no runs.json at all, a document
from before 1.0.6.41 still carrying runs/notify would be migrated back into a
new journal at the next engine start."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sa02m_rules import engine, store  # noqa: E402

AIR = {"id": "air", "name": "Air", "enabled": True, "type": "logic",
       "template": "air_quality_ventilation",
       "params": {"fans": ["fan"], "co2_sensors": ["co2"], "boost_delay_s": 0, "settle_s": 10}}
BLOCK = {"id": "s1", "name": "s1", "enabled": True, "type": "block",
         "trigger": [{"kind": "state", "device": "lamp", "cap": "on_off", "op": "changed"}],
         "condition": {},
         "action": [{"kind": "set", "device": "led", "cap": "on_off", "value": 1}]}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _engine(path):
    return engine.Engine(lambda *_a: None, path, now=lambda: 100000.0, pub_state=lambda *_a: None)


class AfterFactoryReset(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        self.journal = store.journal_path(self.path)

    def _reset_shape(self, doc):
        """What the runner leaves: the document as it was, the empty journal,
        no logic_state.json."""
        _write(self.path, doc)
        _write(self.journal, store.empty_journal())
        self.assertFalse(os.path.exists(store.logic_state_path(self.path)))

    def test_a_engine_boots_on_the_reset_store_with_every_scenario_and_fresh_templates(self):
        self._reset_shape({"scenarios": [BLOCK, AIR], "library": "", "vars": {}})
        e = _engine(self.path)
        self.assertEqual(e._unarmed, set(), "every kept scenario is armed")
        self.assertIn("air", e._logic, "the logic template is adopted")
        doc = store.load(self.path)
        self.assertEqual((doc["runs"], doc["notify_queue"]), ([], []))
        self.assertEqual([(r["id"], r["last_run"], r["last_error"]) for r in store.listed(doc)],
                         [("air", None, ""), ("s1", None, "")])
        # Fresh = what an engine on a store that never had a journal or a state
        # file holds and writes for the same template.
        with tempfile.TemporaryDirectory() as clean:
            cpath = os.path.join(clean, "scenarios.json")
            _write(cpath, {"scenarios": [BLOCK, AIR], "library": "", "vars": {}})
            fresh = _engine(cpath)
            want_file = store.read_logic_state(cpath)
            want_state = fresh._logic["air"].export_state()
        self.assertEqual(e._logic["air"].export_state(), want_state)
        self.assertEqual(store.read_logic_state(self.path), want_file)

    def test_a_contrast_a_kept_state_file_is_restored(self):
        """Why the runner removes logic_state.json: left in place, the previous
        owner's template state is adopted (same board, same template, same id)."""
        self._reset_shape({"scenarios": [AIR], "library": "", "vars": {}})
        donor = _engine(self.path)
        donor.on_state("fan", "on_off", 0)
        donor.on_state("fan", "fan_speed", 45)
        donor.on_state("co2", "co2", 1400)
        donor.flush_logic_state()
        exported = donor._logic["air"].export_state()
        self.assertIn("air", store.read_logic_state(self.path))
        self.assertEqual(_engine(self.path)._logic["air"].export_state(), exported)

    def test_b_empty_journal_blocks_the_legacy_migration_and_strips_the_document(self):
        legacy = {"scenarios": [dict(BLOCK, last_run=5.0, last_error="donor")], "library": "", "vars": {},
                  "runs": [{"ts": 5.0, "id": "s1", "name": "s1", "error": "donor", "ok": False,
                            "source": "scenario"}],
                  "notify_queue": [{"ts": 5.0, "text": "donor note"}]}
        self._reset_shape(legacy)
        e = _engine(self.path)
        self.assertEqual(_read(self.journal), store.empty_journal(),
                         "the legacy runs/notify are not copied into the journal")
        raw = _read(self.path)
        self.assertNotIn("runs", raw)
        self.assertNotIn("notify_queue", raw)
        self.assertNotIn("last_run", raw["scenarios"][0])
        self.assertNotIn("last_error", raw["scenarios"][0])
        self.assertEqual(raw["scenarios"][0]["trigger"], BLOCK["trigger"], "the scenario itself is kept")
        self.assertEqual((e.doc["runs"], e.doc["notify_queue"]), ([], []))
        self.assertIsNone(store.listed(store.load(self.path))[0]["last_run"])

    def test_b_contrast_without_a_journal_the_legacy_runs_come_back(self):
        """Why the runner writes the empty journal instead of deleting runs.json."""
        legacy = {"scenarios": [BLOCK], "library": "", "vars": {},
                  "runs": [{"ts": 5.0, "id": "s1", "name": "s1", "error": "donor", "ok": False,
                            "source": "scenario"}],
                  "notify_queue": []}
        _write(self.path, legacy)
        _engine(self.path)
        self.assertEqual([r["error"] for r in _read(self.journal)["runs"]], ["donor"])


if __name__ == "__main__":
    unittest.main()
