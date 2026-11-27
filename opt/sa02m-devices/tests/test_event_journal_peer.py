"""Strict consumer observation and external active-only transition."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from sa02m_devices import event_journal_peer as peer
from sa02m_devices.event_journal_state import JournalStateError


COMMON = ("Id", "LoadState", "ActiveState", "SubState", "Job")
SERVICE = ("UnitFileState", "MainPID", "Environment", "EnvironmentFiles")


def properties(unit, *, service=False, **changes):
    result = dict.fromkeys(COMMON, "")
    result.update(Id=unit, LoadState="loaded", ActiveState="active", SubState="running")
    if service:
        result.update(UnitFileState="enabled", MainPID="10", Environment="", EnvironmentFiles="")
    result.update(changes)
    return result


class Manager:
    """Only emit fields implemented by the modeled interface, like systemctl."""
    def __init__(self):
        self.units = {name: properties(name, service=True) for name in peer.UNITS.values()}
        self.calls = []
        self.dispatched = False
        self.transform = lambda unit, text: text

    def command(self, argv, deadline, limit=65536, *, executable="systemctl"):
        peer._remaining(deadline)
        self.calls.append(argv)
        if argv == ["--no-block", "try-restart", peer.PEER]:
            self.dispatched = True
            self.units[peer.PEER]["MainPID"] = "20"
            return ""
        assert argv[0] == "show", argv
        unit = argv[-1] if "--" in argv else argv[1]
        keys = next(arg.removeprefix("--property=") for arg in argv if arg.startswith("--property=")).split(",")
        info = self.units[unit]
        text = "".join(key + "=" + info[key] + "\n" for key in keys if key in info)
        return self.transform(unit, text)

    def identity(self, pid):
        return (pid, b"200" if self.dispatched else b"100"), {}


class PropertyTests(unittest.TestCase):
    def test_common_interfaces_and_option_terminator(self):
        for unit in ("network.target", "systemd-journald.socket", "-.mount", "-.slice", "system.slice"):
            with self.subTest(unit=unit):
                manager = Manager()
                manager.units[unit] = properties(unit)
                with patch.object(peer, "command", manager.command):
                    actual, size = peer.show(unit, time.monotonic() + 5, _service=False)
                self.assertEqual(actual, manager.units[unit])
                self.assertEqual(size, sum(len((key + "=" + value + "\n").encode()) for key, value in actual.items()))
                self.assertEqual(manager.calls[0][-2:], ["--", unit])
                keys = next(a.split("=", 1)[1] for a in manager.calls[0] if a.startswith("--property="))
                self.assertEqual(set(keys.split(",")), set(COMMON))
                self.assertFalse(set(keys.split(",")) & set(SERVICE))

    def test_every_required_service_property_remains_mandatory(self):
        for key in COMMON + SERVICE:
            with self.subTest(key=key):
                manager = Manager()
                del manager.units[peer.PEER][key]
                with patch.object(peer, "command", manager.command), self.assertRaises(JournalStateError):
                    peer.show(peer.PEER, time.monotonic() + 5)

    def test_invalid_unit_never_executes_command(self):
        for unit in ("bad unit", "../unit", "x;touch", "x\n.service", "", '"x.service"'):
            with self.subTest(unit=unit), patch.object(peer, "command") as command, self.assertRaises(JournalStateError):
                peer.show(unit, time.monotonic() + 5, _service=False)
            command.assert_not_called()

    def test_real_subprocess_rejects_ambiguous_argv_and_omits_service_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "properties.json"
            data.write_text(json.dumps(properties("-.mount")))
            executable = root / "systemctl"
            executable.write_text('''#!/usr/bin/python3
import json, os, sys
args = sys.argv[1:]
assert args[0] == 'show' and args[-2:] == ['--', '-.mount'], args
assert all(arg.startswith('--') for arg in args[1:-2]), args
values = json.load(open(os.environ['PEER_PROPERTIES']))
keys = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--property=')).split(',')
for key in keys:
    if key in values:
        print(key + '=' + values[key])
''')
            executable.chmod(0o755)
            with patch.dict(os.environ, {"PATH": folder, "PEER_PROPERTIES": str(data)}):
                actual, _ = peer.show("-.mount", time.monotonic() + 5, _service=False)
            self.assertEqual(actual, properties("-.mount"))


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.manager = Manager()
        self.marked = []
        self.addCleanup(patch.stopall)
        patch.object(peer, "command", self.manager.command).start()
        patch.object(peer, "process_identity", self.manager.identity).start()

    def prepare(self, expected=None):
        peer.transition(expected or {}, lambda: self.marked.append(True), force_refresh=True)

    def refuse(self):
        with self.assertRaises(JournalStateError):
            self.prepare()
        self.assertFalse(self.manager.dispatched)

    def test_observation_never_dispatches_and_only_queries_fixed_consumers(self):
        self.manager.units.update({f"extra-{n}.service": properties("ignored") for n in range(1000)})
        peer.observe({})
        self.assertFalse(self.manager.dispatched)
        self.assertEqual({argv[-1] for argv in self.manager.calls}, set(peer.UNITS.values()))
        self.prepare()
        self.assertTrue(self.manager.dispatched)
        self.assertEqual(self.marked, [True])

    def test_each_common_and_loaded_service_property_refuses_before_dispatch(self):
        for unit in peer.UNITS.values():
            for key in COMMON + SERVICE:
                original = self.manager.units[unit].pop(key)
                with self.subTest(unit=unit, missing=key):
                    self.refuse()
                self.manager.units[unit][key] = original

    def test_duplicate_unknown_malformed_properties_and_aliases_refuse(self):
        for extra in ("Job=\n", "Unexpected=secret-value\n", "invalid-line\n"):
            self.manager.transform = lambda unit, text: text + extra if unit == peer.PEER else text
            with self.subTest(extra=extra), self.assertRaises(JournalStateError) as caught:
                self.prepare()
            self.assertNotIn("secret-value", str(caught.exception))
            self.assertFalse(self.manager.dispatched)
        self.manager.transform = lambda unit, text: text
        self.manager.units[peer.PEER]["Id"] = "alias.service"
        self.refuse()

    def test_absent_stand_and_inactive_masks_preserve_service_policy(self):
        for load in ("not-found", "masked"):
            for unit in peer.UNITS.values():
                manager = Manager()
                manager.units[unit] = properties(unit, LoadState=load, ActiveState="inactive", SubState="dead")
                with patch.object(peer, "command", manager.command), patch.object(peer, "process_identity", manager.identity):
                    if load == "not-found" and unit != peer.PEER:
                        with self.assertRaises(JournalStateError):
                            self.prepare()
                        self.assertFalse(manager.dispatched)
                        continue
                    self.prepare()
                self.assertEqual(manager.dispatched, unit != peer.PEER)
                selected = [argv for argv in manager.calls if argv[0] == "show" and argv[-1] == unit]
                self.assertEqual(len(selected), 1)
                self.assertTrue(all("EnvironmentFiles" not in argument for argument in selected[0]))

    def test_absent_optional_stand_requires_missing_or_zero_main_pid(self):
        self.manager.units[peer.PEER] = properties(peer.PEER, LoadState="not-found",
                                                 ActiveState="inactive", SubState="dead", MainPID="0")
        self.assertTrue(peer.observe({})[-1])
        self.manager.units[peer.PEER]["MainPID"] = "23"
        self.refuse()

    def test_unavailable_contradictions_and_partial_common_refuse(self):
        for load in ("not-found", "masked"):
            base = properties(peer.PEER, LoadState=load, ActiveState="inactive", SubState="dead")
            variants = [{**base, key: value} for key, value in (("ActiveState", "active"), ("ActiveState", "failed"),
                        ("ActiveState", "activating"), ("SubState", "running"), ("Job", "42"), ("Id", "alias.service"))]
            variants += [{key: value for key, value in base.items() if key != missing} for missing in COMMON]
            for info in variants:
                self.manager.units[peer.PEER] = info
                with self.subTest(info=info):
                    self.refuse()

    def test_loaded_stopped_and_failed_peer_remain_stopped(self):
        for active in ("inactive", "failed"):
            self.manager.units[peer.PEER] = properties(peer.PEER, service=True, ActiveState=active)
            self.prepare()
            self.assertFalse(self.manager.dispatched)
        self.manager.units[peer.PEER] = properties(peer.PEER, service=True, UnitFileState="masked")
        self.refuse()

    def test_wrong_effective_settings_and_process_identity_refuse(self):
        expected = {"STAND_DEVICES_EVENTS_PROTOCOL": "1"}
        for info in self.manager.units.values():
            info["Environment"] = "STAND_DEVICES_EVENTS_PROTOCOL=1"
        for unit in peer.UNITS.values():
            self.manager.units[unit]["Environment"] = "STAND_DEVICES_EVENTS_PROTOCOL=wrong"
            with self.subTest(unit=unit), self.assertRaises(JournalStateError):
                self.prepare(expected)
            self.assertFalse(self.manager.dispatched)
            self.manager.units[unit]["Environment"] = "STAND_DEVICES_EVENTS_PROTOCOL=1"
        with patch.object(peer, "process_identity", side_effect=JournalStateError("bad process identity")), self.assertRaises(JournalStateError):
            self.prepare(expected)
        self.assertFalse(self.manager.dispatched)

    def test_replacement_requires_new_birth_and_matching_environment(self):
        expected = {"STAND_DEVICES_EVENTS_PROTOCOL": "1"}
        for info in self.manager.units.values():
            info["Environment"] = "STAND_DEVICES_EVENTS_PROTOCOL=1"
        with self.assertRaises(JournalStateError):
            self.prepare(expected)
        self.assertTrue(self.manager.dispatched)
        ticks = [time.monotonic()]
        def monotonic():
            if self.manager.dispatched:
                ticks[0] += 20
            return ticks[0]
        self.manager.dispatched = False
        with patch.object(peer, "process_identity", return_value=(("10", b"100"), expected)), \
                patch.object(peer.time, "monotonic", monotonic), patch.object(peer.time, "sleep"), \
                self.assertRaisesRegex(JournalStateError, "deadline"):
            self.prepare(expected)

    def test_operator_stop_or_mask_during_transition_refuses(self):
        original = self.manager.command
        def stopped(argv, *args, **kwargs):
            text = original(argv, *args, **kwargs)
            if self.manager.dispatched:
                self.manager.units[peer.PEER]["ActiveState"] = "inactive"
            return text
        with patch.object(peer, "command", stopped), self.assertRaisesRegex(JournalStateError, "unavailable"):
            self.prepare()

    def test_shared_probe_byte_and_time_bounds_are_not_per_command(self):
        budget = peer.Budget(time.monotonic() + 5)
        budget.consume(b"x" * 65535)
        with self.assertRaises(JournalStateError):
            peer.observe({}, deadline=budget.deadline, budget=budget)
        with self.assertRaises(JournalStateError):
            peer.observe({}, deadline=time.monotonic() - 1)


if __name__ == "__main__":
    unittest.main()
