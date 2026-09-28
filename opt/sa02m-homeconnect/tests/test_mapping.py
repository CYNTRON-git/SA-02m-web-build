"""mapping.py: every MAPPING row, refusals, id/name sanitisers."""

from __future__ import annotations

import unittest

from sa02m_homeconnect import mapping as M

E = "BSH.Common.EnumType."


class MappingRowsTest(unittest.TestCase):
    def test_every_control_is_reachable(self) -> None:
        produced = set()
        produced.update(n for n, _ in M.connected_update(True))
        samples = [
            (M.K_POWER, E + "PowerState.On"),
            (M.K_DOOR, E + "DoorState.Open"),
            (M.K_OPSTATE, E + "OperationState.Run"),
            (M.K_REMOTE_START, True),
            (M.K_REMOTE_ACTIVE, False),
            (M.K_LOCAL_ACTIVE, True),
            (M.K_ACTIVE_PROGRAM, "Dishcare.Dishwasher.Program.Eco50"),
            (M.K_REMAINING, 3600),
            (M.K_PROGRESS, 42),
            ("BSH.Common.Event.ProgramFinished", E + "EventPresentState.Present"),
        ]
        for key, value in samples:
            produced.update(n for n, _ in M.apply_item(key, value, ts=1700000000))
        self.assertEqual(produced, set(M.CONTROLS))

    def test_values(self) -> None:
        self.assertEqual(M.apply_item(M.K_POWER, E + "PowerState.Standby"), [("power_on", "0")])
        self.assertEqual(M.apply_item(M.K_DOOR, E + "DoorState.Locked"), [("door_open", "0")])
        self.assertEqual(M.apply_item(M.K_OPSTATE, E + "OperationState.Finished"),
                         [("operation_state", "Finished"), ("running", "0"), ("finished", "1")])
        self.assertEqual(M.apply_item(M.K_REMAINING, 59.6), [("remaining_s", "60")])
        self.assertEqual(M.apply_item(M.K_ACTIVE_PROGRAM, None), [("active_program", "")])
        self.assertEqual(M.apply_item("BSH.Common.Event.ProgramAborted", None, ts=5),
                         [("last_event", "ProgramAborted"), ("last_event_ts", "5")])

    def test_refusals_publish_nothing(self) -> None:
        refused = [
            (M.K_POWER, E + "PowerState.Exploded"),     # unknown enum value
            (M.K_POWER, "On"),                          # not the enum grammar
            (M.K_DOOR, 1),
            (M.K_REMOTE_START, "true"),                 # string, not a JSON bool
            (M.K_REMAINING, -1),
            (M.K_REMAINING, float("nan")),
            (M.K_REMAINING, True),
            (M.K_PROGRESS, 101),
            (M.K_OPSTATE, E + "OperationState.Run; rm"),
            (M.K_ACTIVE_PROGRAM, "Program/../../x"),
            ("BSH.Common.Event.ProgramFinished", E + "EventPresentState.Confirmed"),
            ("Some.Unknown.Key", 1),
            (None, 1),
        ]
        for key, value in refused:
            self.assertEqual(M.apply_item(key, value, ts=1), [], (key, value))

    def test_event_without_timestamp_publishes_nothing(self) -> None:
        self.assertEqual(M.apply_item("BSH.Common.Event.ProgramFinished", None, ts=None), [])

    def test_program_updates_maps_options(self) -> None:
        upd = M.program_updates({"key": "LaundryCare.Washer.Program.Cotton", "options": [
            {"key": M.K_REMAINING, "value": 1200, "unit": "seconds"},
            {"key": M.K_PROGRESS, "value": 10},
            {"key": "LaundryCare.Washer.Option.Temperature", "value": "x"},
        ]})
        self.assertEqual(upd, [("active_program", "Cotton"), ("remaining_s", "1200"),
                               ("progress_pct", "10")])


class SanitiserTest(unittest.TestCase):
    def test_device_id(self) -> None:
        self.assertEqual(M.device_id("SIEMENS-HCS02DWH1-6BE58C3B3B35"), "hc-siemens-hcs02dwh1-6be58c3b3b35")
        self.assertEqual(M.device_id("a/b#c+d e"), "hc-a-b-c-d-e")
        self.assertLessEqual(len(M.device_id("X" * 200)), 64)
        with self.assertRaises(ValueError):
            M.device_id("///")
        for bad in ("a/b", "#", "+", " "):
            self.assertNotIn(bad, M.device_id("x" + bad + "y"))

    def test_clean_name(self) -> None:
        self.assertEqual(M.clean_name("  Посудомойка\n‮ кухня\x00 ", "fb"), "Посудомойка кухня")
        self.assertEqual(M.clean_name("", "fb"), "fb")
        self.assertEqual(M.clean_name(None, "fb"), "fb")
        self.assertLessEqual(len(M.clean_name("я" * 500, "fb")), 64)

    def test_clean_type(self) -> None:
        self.assertEqual(M.clean_type("Dishwasher"), "Dishwasher")
        self.assertEqual(M.clean_type("Dish washer"), "")


if __name__ == "__main__":
    unittest.main()
