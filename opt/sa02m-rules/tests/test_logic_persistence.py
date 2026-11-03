"""Logic-template runtime state survives an engine restart and a same-template
edit (contract cloud-scenarios.md «Room-air logic», «Состояние шаблонов»).

`sa02m-rules` restarts on every OTA (etc/sa02m-update-runner.sh); before the
state file a boosted fan stayed boosted, a re-boost snapshotted the boosted
state as the «original», lights lit by motion were never switched off and
«Я дома» restored nothing (review engine4-port B1)."""
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

AIR = {"fans": ["fan"], "co2_sensors": ["co2"], "boost_delay_s": 0, "settle_s": 10}


def air_row(**params):
    p = dict(AIR)
    p.update(params)
    return {"id": "air", "name": "Air", "enabled": True, "type": "logic",
            "template": "air_quality_ventilation", "params": p}


class Harness:
    """An Engine on a temp store; `restart()` builds a new Engine on the same
    directory — what an OTA service restart does."""

    def __init__(self, test: unittest.TestCase, rows):
        self.td = tempfile.TemporaryDirectory()
        test.addCleanup(self.td.cleanup)
        self.path = os.path.join(self.td.name, "scenarios.json")
        store.save({"scenarios": rows, "library": "", "vars": {}}, self.path)
        self.clock = [100000.0]
        self.pubs = []
        self.e = self._engine()

    def _engine(self):
        e = engine.Engine(lambda d, c, v: self.pubs.append((d, c, v)), self.path,
                          now=lambda: self.clock[0], pub_state=lambda d, c, v: None)
        e.set_caps_provider(lambda d: ("on_off", "fan_speed") if d == "fan" else ("on_off",))
        return e

    def restart(self):
        # What RulesApp.stop() does on SIGTERM before the new process starts.
        self.e.flush_runs()
        self.e.flush_logic_state()
        self.e = self._engine()
        return self.e

    def advance(self, seconds):
        self.clock[0] += seconds
        self.e.tick()

    def state_file(self):
        with open(store.logic_state_path(self.path), encoding="utf-8") as fh:
            return json.load(fh)


class AirQualityRestart(unittest.TestCase):
    def boosted(self):
        h = Harness(self, [air_row()])
        h.e.on_state("fan", "on_off", 0)
        h.e.on_state("fan", "fan_speed", 45)
        h.e.on_state("co2", "co2", 1400)
        self.assertEqual(h.pubs, [("fan", "fan_speed", 90.0), ("fan", "on_off", 1)])
        h.pubs.clear()
        return h

    def test_restore_after_restart_when_air_is_clean(self):
        h = self.boosted()
        e = h.restart()
        # The retained snapshot after the restart: the fan is still boosted.
        e.on_state("fan", "on_off", 1)
        e.on_state("fan", "fan_speed", 90)
        e.on_state("co2", "co2", 600)
        h.advance(11)
        # (The still-active rule may re-assert 90/on first: desired state.)
        self.assertEqual(h.pubs[-2:], [("fan", "fan_speed", 45), ("fan", "on_off", 0)],
                         "the original mode is restored after a service restart")

    def test_restart_while_air_is_still_bad_keeps_the_original_snapshot(self):
        h = self.boosted()
        e = h.restart()
        e.on_state("fan", "on_off", 1)
        e.on_state("fan", "fan_speed", 90)
        e.on_state("co2", "co2", 1400)
        e.on_state("co2", "co2", 600)
        h.advance(11)
        self.assertEqual(h.pubs[-2:], [("fan", "fan_speed", 45), ("fan", "on_off", 0)],
                         "the boosted state must not become the restore snapshot")

    def test_same_template_edit_keeps_the_snapshot(self):
        h = self.boosted()
        store.save({"scenarios": [air_row(settle_s=20)], "library": "", "vars": {}}, h.path)
        h.e.adopt(store.load(h.path))
        h.e.on_state("co2", "co2", 600)
        h.advance(21)
        self.assertEqual(h.pubs[-2:], [("fan", "fan_speed", 45), ("fan", "on_off", 0)])

    def test_state_file_is_beside_the_store_and_dropped_with_the_identity(self):
        h = self.boosted()
        h.advance(store.RUNS_FLUSH_S)  # saved on the journal cadence
        entry = h.state_file()["scenarios"]["air"]
        self.assertEqual(entry["template"], "air_quality_ventilation")
        self.assertEqual(entry["state"]["saved"], {"fan": [0, 45]})
        self.assertIs(entry["state"]["active"], True)
        self.assertEqual(os.path.dirname(store.logic_state_path(h.path)), h.td.name)
        # Another template under the same id is a new identity: nothing carried.
        row = air_row()
        row["template"] = "co2_ventilation"
        store.save({"scenarios": [row], "library": "", "vars": {}}, h.path)
        h.e.adopt(store.load(h.path))
        self.assertNotIn("air", h.state_file()["scenarios"])
        store.save({"scenarios": [air_row()], "library": "", "vars": {}}, h.path)
        h.e.adopt(store.load(h.path))
        self.assertIs(h.e._logic["air"]._active, False)
        self.assertEqual(h.e._logic["air"]._saved, {})

    def test_disable_and_delete_drop_the_state(self):
        h = self.boosted()
        row = air_row()
        row["enabled"] = False
        store.save({"scenarios": [row], "library": "", "vars": {}}, h.path)
        h.e.adopt(store.load(h.path))
        self.assertEqual(h.state_file()["scenarios"], {})
        h2 = self.boosted()
        store.save({"scenarios": [], "library": "", "vars": {}}, h2.path)
        h2.e.adopt(store.load(h2.path))
        self.assertEqual(h2.state_file()["scenarios"], {})

    def test_a_fan_removed_by_an_edit_is_not_carried(self):
        h = self.boosted()
        store.save({"scenarios": [air_row(fans=["other"])], "library": "", "vars": {}}, h.path)
        h.e.adopt(store.load(h.path))
        self.assertEqual(h.e._logic["air"]._saved, {})


class FailSafe(unittest.TestCase):
    def assert_fresh_start(self, content):
        h = Harness(self, [air_row()])
        with open(store.logic_state_path(h.path), "w", encoding="utf-8") as fh:
            fh.write(content)
        with self.assertLogs("sa02m-rules", "WARNING"):
            e = h.restart()
        inst = e._logic["air"]
        self.assertIs(inst._active, False)
        self.assertEqual(inst._saved, {})

    def test_corrupt_or_foreign_file_is_ignored_and_logged(self):
        for content in ("{not json", "[]", '{"version": 99, "scenarios": {}}',
                        '{"version": 1, "scenarios": []}', "[" * 100000):
            with self.subTest(content=content[:20]):
                self.assert_fresh_start(content)

    def test_invalid_entry_is_ignored_and_logged(self):
        for state in ({"active": "yes", "saved": {}, "paused": {}},
                      {"active": True, "saved": {"fan": [0, "fast"]}, "paused": {}},
                      {"active": True, "saved": {"fan": [None, 45]}, "paused": {}},
                      {"active": True, "saved": {"fan": [0]}, "paused": {}},
                      {"active": True, "saved": {}, "paused": {"fan": "soon"}},
                      {"active": True, "saved": {"fan": [0, float("nan")]}, "paused": {}}):
            with self.subTest(state=state):
                self.assert_fresh_start(json.dumps(
                    {"version": 1, "scenarios": {"air": {"template": "air_quality_ventilation",
                                                         "state": state}}}))

    def test_unwritable_state_never_stops_the_engine(self):
        h = Harness(self, [air_row()])
        lpath = store.logic_state_path(h.path)
        if os.path.exists(lpath):
            os.remove(lpath)
        os.mkdir(lpath)  # a directory where the file goes: every save fails
        h.e.on_state("fan", "on_off", 0)
        h.e.on_state("fan", "fan_speed", 45)
        h.e.on_state("co2", "co2", 1400)
        self.assertEqual(h.pubs, [("fan", "fan_speed", 90.0), ("fan", "on_off", 1)])
        with self.assertLogs("sa02m-rules", "WARNING"):
            h.advance(store.RUNS_FLUSH_S)  # the save on the cadence fails
        h.advance(store.RUNS_FLUSH_S)  # the retry is not a crash either


class MotionLightRestart(unittest.TestCase):
    def test_lights_lit_before_a_restart_are_switched_off(self):
        row = {"id": "ml", "name": "Motion", "enabled": True, "type": "logic",
               "template": "motion_light", "params": {"sensors": ["pir"], "lights": ["lamp"],
                                                      "off_s": 30}}
        h = Harness(self, [row])
        h.e.on_state("pir", "motion", 0)
        h.e.on_state("pir", "motion", 1)
        self.assertIn(("lamp", "on_off", 1), h.pubs)
        h.pubs.clear()
        e = h.restart()
        e.on_state("lamp", "on_off", 1)
        e.on_state("pir", "motion", 0)
        h.advance(31)
        self.assertEqual(h.pubs, [("lamp", "on_off", 0)])


class AwayHomeRestart(unittest.TestCase):
    def test_arrival_after_a_restart_restores_what_leaving_turned_off(self):
        row = {"id": "away", "name": "Away", "enabled": True, "type": "logic",
               "template": "away_home", "params": {"button": "btn", "lights": ["l1", "l2"],
                                                   "notify": False}}
        h = Harness(self, [row])
        h.e.on_state("l1", "on_off", 1)
        h.e.on_state("l2", "on_off", 0)
        h.e._btn_emit("btn", "di_1", "double")
        self.assertEqual(h.e.home_mode(), "away")
        h.pubs.clear()
        e = h.restart()
        e._btn_emit("btn", "di_1", "double")
        self.assertEqual(e.home_mode(), "home")
        self.assertEqual(h.pubs, [("l1", "on_off", 1)])



def logic_row(sid, template, params):
    return {"id": sid, "name": sid, "enabled": True, "type": "logic",
            "template": template, "params": params}


THRESHOLD = (
    # (template, params, actor, drive-on reading, drive-off reading)
    ("thermostat", {"sensors": ["t"], "heaters": ["heater"], "day_temp": 22, "night_temp": 22,
                    "min_on_s": 0, "min_off_s": 0, "window": "win"},
     "heater", ("t", "temperature", 20), ("t", "temperature", 25)),
    ("humidity_fan", {"sensors": ["rh"], "fans": ["exhaust"], "on_rh": 65, "off_rh": 55},
     "exhaust", ("rh", "humidity", 70), ("rh", "humidity", 50)),
    ("co2_ventilation", {"sensors": ["c"], "fans": ["vent"], "on_ppm": 1000, "off_ppm": 700},
     "vent", ("c", "co2", 1200), ("c", "co2", 600)),
    ("humidifier", {"sensors": ["rh"], "humidifiers": ["hum"], "target_rh": 45, "hysteresis": 5},
     "hum", ("rh", "humidity", 30), ("rh", "humidity", 60)),
)


class ThresholdTemplatesRestart(unittest.TestCase):
    """Review round 2, R2-B1: the «switched on by me» flag of the threshold
    templates survives a restart, reconciled with the actor's first report."""

    def switched_on(self, template, params, actor, on_reading):
        h = Harness(self, [logic_row("rule", template, params)])
        h.e.on_state(*on_reading)
        self.assertIn((actor, "on_off", 1), h.pubs, template)
        h.pubs.clear()
        return h

    def test_actor_switched_on_before_a_restart_is_switched_off(self):
        for template, params, actor, on_reading, off_reading in THRESHOLD:
            with self.subTest(template=template):
                h = self.switched_on(template, params, actor, on_reading)
                e = h.restart()
                e.on_state(actor, "on_off", 1)  # the retained report: still on
                e.on_state(*off_reading)
                self.assertEqual(h.pubs, [(actor, "on_off", 0)])

    def test_actor_reported_off_after_a_restart_is_switched_on_again(self):
        for template, params, actor, on_reading, _off in THRESHOLD:
            with self.subTest(template=template):
                h = self.switched_on(template, params, actor, on_reading)
                e = h.restart()
                e.on_state(actor, "on_off", 0)  # switched off while the service was down
                e.on_state(*on_reading)
                self.assertEqual(h.pubs, [(actor, "on_off", 1)])

    def test_thermostat_window_interlock_acts_after_a_restart(self):
        template, params, actor, on_reading, _off = THRESHOLD[0]
        h = self.switched_on(template, params, actor, on_reading)
        e = h.restart()
        e.on_state(actor, "on_off", 1)
        e.on_state("t", "temperature", 21.8)  # inside the band: no switching
        self.assertEqual(h.pubs, [])
        e.on_state("win", "open", 1)
        self.assertEqual(h.pubs, [(actor, "on_off", 0)])

    def test_one_actor_still_on_keeps_the_flag(self):
        params = {"sensors": ["c"], "fans": ["v1", "v2"], "on_ppm": 1000, "off_ppm": 700}
        h = Harness(self, [logic_row("rule", "co2_ventilation", params)])
        h.e.on_state("c", "co2", 1200)
        h.pubs.clear()
        e = h.restart()
        e.on_state("v1", "on_off", 0)
        e.on_state("v2", "on_off", 1)
        e.on_state("c", "co2", 600)
        self.assertEqual(h.pubs, [("v1", "on_off", 0), ("v2", "on_off", 0)])
        h2 = Harness(self, [logic_row("rule", "co2_ventilation", params)])
        h2.e.on_state("c", "co2", 1200)
        h2.pubs.clear()
        e2 = h2.restart()
        e2.on_state("v1", "on_off", 0)
        e2.on_state("v2", "on_off", 0)
        e2.on_state("c", "co2", 1300)
        self.assertEqual(h2.pubs, [("v1", "on_off", 1), ("v2", "on_off", 1)])

    def test_humidity_fan_run_time_counts_from_the_original_switch_on(self):
        template, params, actor, on_reading, _off = THRESHOLD[1]
        h = self.switched_on(template, dict(params, max_run_min=30), actor, on_reading)
        h.clock[0] += 10 * 60
        e = h.restart()
        e.on_state(actor, "on_off", 1)
        e.on_state("rh", "humidity", 72)
        self.assertEqual(h.pubs, [], "10 of 30 minutes run: the fan stays on")
        h.clock[0] += 21 * 60
        e.on_state("rh", "humidity", 74)
        self.assertEqual(h.pubs, [(actor, "on_off", 0)])

    def test_another_template_under_the_same_id_starts_fresh(self):
        """Review round 2, A3: co2_ventilation and humidifier export the same
        shape ({"on": …}); only the identity check keeps one from adopting the
        other's flag."""
        h = Harness(self, [logic_row("rule", "co2_ventilation",
                                     {"sensors": ["c"], "fans": ["dev"], "on_ppm": 1000, "off_ppm": 700})])
        h.e.on_state("c", "co2", 1200)
        self.assertIs(h.e._logic["rule"]._on, True)
        row = logic_row("rule", "humidifier", {"sensors": ["rh"], "humidifiers": ["dev"],
                                               "target_rh": 45, "hysteresis": 5})
        store.save({"scenarios": [row], "library": "", "vars": {}}, h.path)
        h.e.adopt(store.load(h.path))
        self.assertIs(h.e._logic["rule"]._on, False)


class MotionLightOffTimer(unittest.TestCase):
    def test_restored_lit_light_goes_off_without_a_motion_report(self):
        """Review round 2, A4: a non-retained motion source sends nothing after
        the restart; the restored light must still go off after off_s."""
        row = {"id": "ml", "name": "Motion", "enabled": True, "type": "logic",
               "template": "motion_light", "params": {"sensors": ["pir"], "lights": ["lamp"],
                                                      "off_s": 30}}
        h = Harness(self, [row])
        h.e.on_state("pir", "motion", 1)
        h.pubs.clear()
        h.restart()
        h.advance(29)
        self.assertEqual(h.pubs, [])
        h.advance(2)
        self.assertEqual(h.pubs, [("lamp", "on_off", 0)])
        h2 = Harness(self, [row])
        h2.e.on_state("pir", "motion", 1)
        h2.pubs.clear()
        e = h2.restart()
        e.on_state("pir", "motion", 1)  # motion still there: the timer is cancelled
        h2.advance(31)
        self.assertNotIn(("lamp", "on_off", 0), h2.pubs)


class SaveCadence(unittest.TestCase):
    """Review round 2, A1/A2: saves coalesce on the RUNS_FLUSH_S cadence and
    the service stop saves what is pending."""

    ROW = {"id": "away", "name": "Away", "enabled": True, "type": "logic",
           "template": "away_home", "params": {"button": "btn", "lights": ["l1"], "notify": False}}

    def test_a_burst_of_transitions_is_one_save(self):
        from unittest.mock import patch
        h = Harness(self, [self.ROW])
        h.e.on_state("l1", "on_off", 1)
        real = engine.save_logic_state
        calls = []

        def counting(*args):
            calls.append(1)
            return real(*args)

        with patch.object(engine, "save_logic_state", side_effect=counting):
            for _ in range(10):  # leave / arrive, ten times inside one second
                h.e._btn_emit("btn", "di_1", "double")
                h.e._btn_emit("btn", "di_1", "double")
                h.e.on_state("l1", "on_off", 0)
                h.e.on_state("l1", "on_off", 1)
            h.e._btn_emit("btn", "di_1", "double")  # leave once more: l1 recorded
            self.assertLessEqual(len(calls), 1, "a save per transition (A16 class)")
            h.advance(store.RUNS_FLUSH_S)
            self.assertEqual(len(calls), 1)
        self.assertEqual(h.state_file()["scenarios"]["away"]["state"], {"turned_off": ["l1"]})

    def test_service_stop_saves_pending_state(self):
        from unittest.mock import Mock, patch
        from sa02m_rules import service
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        store.save({"scenarios": [self.ROW], "library": "", "vars": {}}, path)
        with patch.object(service, "ALICE_DEVICES", os.path.join(td.name, "none.json")):
            app = service.RulesApp(Mock(), path)
        app.engine.on_state("l1", "on_off", 1)
        with patch.object(engine, "save_logic_state", side_effect=OSError("disk")):
            with self.assertLogs("sa02m-rules", "WARNING"):
                app.engine._btn_emit("btn", "di_1", "double")
                app.engine.flush_logic_state()
        app.stop()
        with open(store.logic_state_path(path), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["scenarios"]["away"]["state"], {"turned_off": ["l1"]})

    def test_interrupted_save_leaves_no_temp_file(self):
        from unittest.mock import patch
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = os.path.join(td.name, "scenarios.json")
        with patch.object(store.os, "fsync", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit):
                store.save_logic_state({}, path)
        self.assertEqual([n for n in os.listdir(td.name) if n.startswith(".rules-")], [])


class Round3(unittest.TestCase):
    """Review round 3: R3-B1, R3-B2, the immediate save of a switch-on, X4, X8."""

    ML = {"id": "ml", "name": "Motion", "enabled": True, "type": "logic",
          "template": "motion_light", "params": {"sensors": ["pir"], "lights": ["lamp"],
                                                 "off_s": 30}}

    def test_same_template_edit_with_motion_present_keeps_the_light_on(self):
        """R3-B1: an edit re-creates the instance with motion=1 already in the
        mirror; a steady sensor sends no new edge, so a timer armed at boot
        would switch an occupied room dark (contract: timing starts after all
        sensor inputs clear)."""
        h = Harness(self, [self.ML])
        h.e.on_state("pir", "motion", 0)
        h.e.on_state("pir", "motion", 1)
        h.pubs.clear()
        row = dict(self.ML, params=dict(self.ML["params"], off_s=40))
        store.save({"scenarios": [row], "library": "", "vars": {}}, h.path)
        h.e.adopt(store.load(h.path))
        self.assertIs(h.e._logic["ml"]._lit, True)
        h.advance(45)
        self.assertNotIn(("lamp", "on_off", 0), h.pubs)
        h.e.on_state("pir", "motion", 0)  # the room empties: the timer starts now
        h.advance(41)
        self.assertEqual(h.pubs[-1], ("lamp", "on_off", 0))

    def test_saves_follow_the_cadence_while_the_state_keeps_changing(self):
        """R3-B2: one change per second. No save before RUNS_FLUSH_S after the
        first unsaved change (not every tick), one when it is reached, and a
        rule that never stops changing is still saved every RUNS_FLUSH_S (the
        clock does not restart on every change)."""
        from unittest.mock import patch
        row = {"id": "away", "name": "Away", "enabled": True, "type": "logic",
               "template": "away_home", "params": {"button": "btn", "lights": ["l1"],
                                                   "notify": False}}
        h = Harness(self, [row])
        h.e.on_state("l1", "on_off", 1)
        real = engine.save_logic_state
        calls = []

        def counting(*args):
            calls.append(h.clock[0])
            return real(*args)

        flush_s = store.RUNS_FLUSH_S
        with patch.object(engine, "save_logic_state", side_effect=counting):
            h.e._btn_emit("btn", "di_1", "double")  # leave: the first unsaved change
            seen = []
            for _ in range(2 * flush_s):
                h.advance(1)
                seen.append(len(calls))
                h.e._btn_emit("btn", "di_1", "double")  # arrive / leave, every second
        self.assertEqual(seen[:flush_s - 1], [0] * (flush_s - 1), "saved before the cadence")
        self.assertEqual(seen[flush_s - 1], 1)
        self.assertEqual(seen[2 * flush_s - 1], 2, "a rule changing every second is never saved")

    def test_a_switch_on_is_saved_at_once(self):
        """Orchestrator call (round 3): a crash inside the cadence window after
        a thermostat switched a heater on must not leave the heater unowned."""
        template, params, actor, on_reading, off_reading = THRESHOLD[0]
        h = Harness(self, [logic_row("rule", template, params)])
        h.e.on_state(*on_reading)
        self.assertIn((actor, "on_off", 1), h.pubs)
        h.pubs.clear()
        h.e = h._engine()  # a crash: no stop(), no cadence tick
        h.e.on_state(actor, "on_off", 1)
        h.e.on_state(*off_reading)
        self.assertEqual(h.pubs, [(actor, "on_off", 0)])

    def test_same_template_edit_reconciles_with_known_actor_states(self):
        """X4: after an edit the actors' reports are already in the mirror; a
        fan switched off by hand meanwhile clears the flag, so the next
        crossing switches it on again."""
        params = {"sensors": ["c"], "fans": ["vent"], "on_ppm": 1000, "off_ppm": 700}
        h = Harness(self, [logic_row("rule", "co2_ventilation", params)])
        h.e.on_state("c", "co2", 1200)
        h.e.on_state("vent", "on_off", 1)
        h.e.on_state("vent", "on_off", 0)  # switched off by hand
        h.pubs.clear()
        row = logic_row("rule", "co2_ventilation", dict(params, off_ppm=650))
        store.save({"scenarios": [row], "library": "", "vars": {}}, h.path)
        h.e.adopt(store.load(h.path))
        h.e.on_state("c", "co2", 1300)
        self.assertEqual(h.pubs, [("vent", "on_off", 1)])

    def test_humidity_fan_pause_after_max_run_survives_a_restart(self):
        """X8: after max_run_min the fan stays off until humidity really drops
        (cooldown) — also across a restart, when the rule is no longer «on»."""
        template, params, actor, on_reading, _off = THRESHOLD[1]
        h = Harness(self, [logic_row("rule", template, dict(params, max_run_min=30))])
        h.e.on_state(*on_reading)
        h.clock[0] += 31 * 60
        h.e.on_state("rh", "humidity", 72)
        self.assertEqual(h.pubs[-1], (actor, "on_off", 0))
        h.pubs.clear()
        e = h.restart()
        e.on_state(actor, "on_off", 0)
        e.on_state("rh", "humidity", 75)
        self.assertEqual(h.pubs, [], "the max-run pause was lost")
        e.on_state("rh", "humidity", 50)
        e.on_state("rh", "humidity", 70)
        self.assertEqual(h.pubs, [(actor, "on_off", 1)])


if __name__ == "__main__":
    unittest.main()
