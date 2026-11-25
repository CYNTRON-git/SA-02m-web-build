"""Real property parsing and action-directed bounded manager evidence."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from sa02m_devices import event_journal_peer as peer
from sa02m_devices.event_journal_state import JournalStateError


RELATIONS = ("After", "Before", "Requires", "Wants", "Requisite", "BindsTo",
             "ConsistsOf", "PropagatesStopTo", "Conflicts", "ConflictedBy", "OnFailure",
             "RequiredBy", "BoundBy")
COMMON = ("Id", "LoadState", "ActiveState", "UnitFileState", "Job") + RELATIONS
SERVICE = ("MainPID", "Environment", "EnvironmentFiles")
CURRENT = peer.UNITS["devices-api"]


def properties(unit, *, service=False, **changes):
    result = dict.fromkeys(COMMON, "")
    result.update(Id=unit, LoadState="loaded", ActiveState="active")
    if service:
        result.update(MainPID="10", Environment="", EnvironmentFiles="")
    result.update(changes)
    return result


class Manager:
    """Only emit fields implemented by the modeled interface, like systemctl."""
    def __init__(self):
        self.units = {name: properties(name, service=True) for name in peer.UNITS.values()}
        self.calls = []
        self.dispatched = False
        self.transform = lambda unit, text: text

    def command(self, argv, deadline):
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
        for key in SERVICE:
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


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.manager = Manager()
        self.marked = []
        self.addCleanup(patch.stopall)
        patch.object(peer, "command", self.manager.command).start()
        patch.object(peer, "process_identity", self.manager.identity).start()

    def prepare(self):
        peer.prepare_peer({}, "devices-api", lambda: self.marked.append(True), force_refresh=True)

    def refuse(self):
        with self.assertRaises(JournalStateError):
            self.prepare()
        self.assertFalse(self.manager.dispatched)

    def test_real_parser_graph_prepare_composition(self):
        self.manager.units[peer.PEER]["After"] = "network.target system.slice -.mount systemd-journald.socket"
        for unit in self.manager.units[peer.PEER]["After"].split():
            self.manager.units[unit] = properties(unit)
        self.prepare()
        self.assertEqual(self.marked, [True])
        self.assertTrue(self.manager.dispatched)
        for argv in self.manager.calls:
            if argv[0] == "show" and argv[-1] in peer.UNITS.values():
                keys = next(a.split("=", 1)[1] for a in argv if a.startswith("--property="))
                self.assertTrue(set(SERVICE) <= set(keys.split(",")))

    def test_each_missing_common_property_refuses_before_dispatch(self):
        self.manager.units[peer.PEER]["After"] = "network.target"
        for key in COMMON:
            with self.subTest(key=key):
                self.manager.units["network.target"] = properties("network.target")
                del self.manager.units["network.target"][key]
                self.refuse()

    def test_duplicate_unknown_and_malformed_properties_refuse(self):
        self.manager.units[peer.PEER]["After"] = "network.target"
        self.manager.units["network.target"] = properties("network.target")
        for extra in ("After=\n", "Unexpected=secret-value\n", "invalid-line\n"):
            with self.subTest(extra=extra):
                self.manager.transform = lambda unit, text: text + extra if unit == "network.target" else text
                with self.assertRaises(JournalStateError) as caught:
                    self.prepare()
                self.assertNotIn("secret-value", str(caught.exception))
                self.assertFalse(self.manager.dispatched)

    def test_conflicts_start_but_do_not_cascade_from_stop(self):
        for first in ("Conflicts", "ConflictedBy"):
            for second in ("Conflicts", "ConflictedBy"):
                with self.subTest(first=first, second=second):
                    self.manager.units[peer.PEER] = properties(peer.PEER, service=True, **{first: "shutdown.target"})
                    self.manager.units["shutdown.target"] = properties("shutdown.target", **{second: CURRENT})
                    self.manager.dispatched = False
                    self.prepare()
                    self.assertTrue(self.manager.dispatched)

    def test_direct_and_dependency_start_conflicts_refuse(self):
        for relation in ("Conflicts", "ConflictedBy"):
            for indirect in (False, True):
                with self.subTest(relation=relation, indirect=indirect):
                    self.manager.units[peer.PEER] = properties(peer.PEER, service=True,
                        **({"Requires": "middle.target"} if indirect else {relation: CURRENT}))
                    self.manager.units["middle.target"] = properties("middle.target", **{relation: CURRENT})
                    self.refuse()

    def test_conflict_stop_still_propagates_each_stop_relation(self):
        for conflict in ("Conflicts", "ConflictedBy"):
            for stop in ("ConsistsOf", "PropagatesStopTo", "RequiredBy", "BoundBy"):
                with self.subTest(conflict=conflict, stop=stop):
                    self.manager.units[peer.PEER] = properties(peer.PEER, service=True, **{conflict: "shutdown.target"})
                    self.manager.units["shutdown.target"] = properties("shutdown.target", **{stop: CURRENT})
                    self.refuse()

    def test_bounded_cycles_terminate_and_unsafe_aliases_refuse(self):
        self.manager.units[peer.PEER]["Requires"] = "middle.target"
        self.manager.units["middle.target"] = properties("middle.target", Requires=peer.PEER)
        self.prepare()
        self.assertLess(len(self.manager.calls), 10)
        for changes in ({"Requires": CURRENT}, {"Id": CURRENT}):
            with self.subTest(changes=changes):
                self.manager.dispatched = False
                self.manager.units["middle.target"].update(changes)
                self.refuse()

    def test_node_limit_counts_cached_services(self):
        self.manager.units[peer.PEER]["Requires"] = "node-0.target"
        for number in range(254):
            self.manager.units[f"node-{number}.target"] = properties(f"node-{number}.target", Requires=f"node-{number + 1}.target")
        self.refuse()
        shown = [argv[-1] for argv in self.manager.calls if argv[0] == "show"]
        self.assertEqual(len(shown), 256)
        self.assertNotIn("node-253.target", shown)

    def test_aggregate_bound_counts_utf8_and_cached_service_evidence(self):
        for unit in peer.UNITS.values():
            self.manager.units[unit]["Environment"] = "IGNORED=" + "я" * 11000
        self.refuse()
        self.assertEqual(len(self.manager.calls), 3)

    def test_initial_deadline_is_shared_with_graph_queries(self):
        self.manager.units[peer.PEER]["After"] = "network.target"
        self.manager.units["network.target"] = properties("network.target")
        deadlines = []
        command = self.manager.command
        def delayed(argv, deadline):
            deadlines.append(deadline)
            if len(deadlines) == 4:
                with patch.object(peer.time, "monotonic", return_value=deadline + 0.001):
                    return command(argv, deadline)
            return command(argv, deadline)
        with patch.object(peer, "command", delayed):
            self.refuse()
        self.assertEqual(len(set(deadlines)), 1)

    def test_quoted_unit_name_round_trip_and_transitive_refusal(self):
        escaped = r"system-fix\x2deth.slice"
        self.manager.units[peer.PEER]["After"] = 'network.target "system-fix\\\\x2deth.slice"'
        self.manager.units["network.target"] = properties("network.target")
        self.manager.units[escaped] = properties(escaped)
        self.prepare()
        shown = [argv[-1] for argv in self.manager.calls if argv[0] == "show"]
        self.assertIn(escaped, shown)
        self.assertNotIn("system-fix-eth.slice", shown)
        self.manager.dispatched = False
        self.manager.units[escaped]["After"] = CURRENT
        self.refuse()

    def test_bad_relation_representation_never_dispatches(self):
        for relation in RELATIONS:
            for value in ('"unterminated', '""', '../bad.service', 'bad;unit',
                          r'system-fix\x2deth.slice', r'"system-fix\x2deth.slice"',
                          "network.target  system.slice", "network.target\t"):
                with self.subTest(relation=relation, value=value):
                    self.manager.units[peer.PEER] = properties(peer.PEER, service=True, **{relation: value})
                    self.refuse()

    def test_installer_has_no_invoking_job_but_service_evidence_is_still_strict(self):
        self.manager.units[peer.PEER]["After"] = "network.target -.mount system.slice systemd-journald.socket"
        for unit in self.manager.units[peer.PEER]["After"].split():
            self.manager.units[unit] = properties(unit, After="apparmor.service")
        self.manager.units["apparmor.service"] = properties("apparmor.service", LoadState="not-found", ActiveState="inactive")
        peer.prepare_peer({}, "installer", lambda: self.marked.append(True), force_refresh=True)
        self.assertTrue(self.manager.dispatched)
        self.assertEqual({argv[-1] for argv in self.manager.calls if argv[0] == "show"}, set(peer.UNITS.values()))
        for unit in peer.UNITS.values():
            for key in SERVICE:
                with self.subTest(unit=unit, missing=key):
                    saved = self.manager.units[unit].pop(key)
                    self.manager.dispatched = False
                    with self.assertRaises(JournalStateError):
                        peer.prepare_peer({}, "installer", lambda: None, force_refresh=True)
                    self.assertFalse(self.manager.dispatched)
                    self.manager.units[unit][key] = saved

    def test_installer_does_not_bypass_environment_or_process_checks(self):
        for unit in peer.UNITS.values():
            with self.subTest(unit=unit):
                self.manager.units[unit]["Environment"] = "STAND_DEVICES_EVENTS_STATE_DIR=/wrong"
                with self.assertRaises(JournalStateError):
                    peer.prepare_peer({"STAND_DEVICES_EVENTS_STATE_DIR": "/expected"}, "installer", lambda: None,
                                      force_refresh=True)
                self.assertFalse(self.manager.dispatched)
                self.manager.units[unit]["Environment"] = ""
        with patch.object(peer, "process_identity", side_effect=JournalStateError("bad process identity")), \
                self.assertRaises(JournalStateError):
            peer.prepare_peer({}, "installer", lambda: None, force_refresh=True)
        self.assertFalse(self.manager.dispatched)

    def test_absent_inactive_no_job_is_only_an_ordering_leaf(self):
        self.manager.units[peer.PEER]["After"] = "absent.service"
        self.manager.units["absent.service"] = properties("absent.service", LoadState="not-found", ActiveState="inactive",
                                                         After=CURRENT)
        self.prepare()
        self.assertTrue(self.manager.dispatched)
        for relation in ("Requires", "Wants", "ConsistsOf", "PropagatesStopTo", "RequiredBy", "BoundBy", "Conflicts"):
            with self.subTest(relation=relation):
                self.manager.units[peer.PEER] = properties(peer.PEER, service=True, After="absent.service",
                                                         **{relation: "absent.service"})
                self.manager.dispatched = False
                self.refuse()

    def test_ordering_leaf_requires_complete_consistent_identity_and_state(self):
        self.manager.units[peer.PEER]["After"] = "absent.service"
        base = properties("absent.service", LoadState="not-found", ActiveState="inactive")
        for key in COMMON:
            with self.subTest(missing=key):
                self.manager.units["absent.service"] = dict(base)
                del self.manager.units["absent.service"][key]
                self.refuse()
        for changes in ({"Job": "42"}, {"ActiveState": "active"}, {"ActiveState": "failed"},
                        {"Id": CURRENT}, {"Id": "other.service"}, {"Id": "bad unit"}, {"LoadState": "error"}):
            with self.subTest(changes=changes):
                self.manager.units["absent.service"] = {**base, **changes}
                self.refuse()
        self.manager.units[peer.PEER]["After"] = CURRENT
        self.manager.units[CURRENT] = properties(CURRENT, service=True, LoadState="not-found", ActiveState="inactive")
        self.refuse()

    def test_loaded_no_job_is_not_pruned(self):
        self.manager.units[peer.PEER]["After"] = "middle.target"
        self.manager.units["middle.target"] = properties("middle.target", ActiveState="inactive", Job="", After=CURRENT)
        self.refuse()

    def test_both_service_consumers_refuse_current_and_alias_paths(self):
        for consumer in ("devices-api", "devices-logger"):
            current = peer.UNITS[consumer]
            for indirect in (False, True):
                with self.subTest(consumer=consumer, indirect=indirect):
                    self.manager.units[peer.PEER]["After"] = "middle.target" if indirect else current
                    self.manager.units["middle.target"] = properties("middle.target", Id=current)
                    with self.assertRaises(JournalStateError):
                        peer.prepare_peer({}, consumer, lambda: None, force_refresh=True)
                    self.assertFalse(self.manager.dispatched)

    def test_unknown_consumer_refuses_before_any_command(self):
        for consumer in (None, "", "unknown", "Installer"):
            with self.subTest(consumer=consumer), self.assertRaises(JournalStateError):
                peer.prepare_peer({}, consumer, lambda: None, force_refresh=True)
        self.assertEqual(self.manager.calls, [])

    def test_installer_replacement_requires_new_identity_and_matching_environment(self):
        expected = {"STAND_DEVICES_EVENTS_PROTOCOL": "1"}
        for info in self.manager.units.values():
            info["Environment"] = "STAND_DEVICES_EVENTS_PROTOCOL=1"
        with self.assertRaises(JournalStateError):
            peer.prepare_peer(expected, "installer", lambda: None, force_refresh=True)
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
            peer.prepare_peer(expected, "installer", lambda: None, force_refresh=True)
        self.assertTrue(self.manager.dispatched)

    def test_cached_only_graph_still_obeys_node_and_deadline_bounds(self):
        cache = {unit: (info, 10) for unit, info in self.manager.units.items()}
        with self.assertRaises(JournalStateError):
            peer.graph_safe(CURRENT, dict(cache), time.monotonic() - 1)
        cache.update({f"extra-{n}.target": (properties(f"extra-{n}.target"), 10) for n in range(254)})
        with self.assertRaises(JournalStateError):
            peer.graph_safe(CURRENT, cache, time.monotonic() + 5)
        self.assertFalse(self.manager.calls)


if __name__ == "__main__":
    unittest.main()
