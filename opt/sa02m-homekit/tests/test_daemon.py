"""main.Daemon with fake runner / MQTT / watcher — the lifecycle states, the
setup.json lifetime (running ∧ unpaired only), rebuilds that keep pairings and
the lockout count, standby exits."""

from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import unittest

from sa02m_homekit import constants as C
from sa02m_homekit import identity, main, netif

from ._harness import Harness


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()


class StandbyTests(DaemonCase):
    def test_disabled_conf_exits_0_without_listening(self):
        self.h.conf(enabled=False)
        self.h.start()
        self.assertEqual(self.h.join(), 0)
        self.assertEqual(self.h.state(), C.STATE_DISABLED)
        self.assertEqual(self.h.runners, [])
        self.assertIsNone(self.h.mqtt)

    def test_absent_conf_is_disabled(self):
        self.h.start()
        self.assertEqual(self.h.join(), 0)
        self.assertEqual(self.h.state(), C.STATE_DISABLED)

    def test_missing_dependencies_exit_0_with_missing_deps(self):
        self.h.conf(enabled=True)
        self.h.missing = ["pyhap", "segno"]
        with self.assertLogs("sa02m_homekit", "ERROR"):
            self.h.start()
            self.assertEqual(self.h.join(), 0)
        st = self.h.status_json()
        self.assertEqual(st["state"], C.STATE_MISSING_DEPS)
        self.assertIn("pyhap", st["message"])
        self.assertEqual(self.h.runners, [])


_FAKE_MODULES = {
    "hk_probe_ok.py": "VALUE = 1\n",
    # a package present but one of ITS dependencies absent (the _cffi_backend case)
    "hk_probe_subdep.py": "import hk_probe_absent_leaf_zz9  # noqa: F401\n",
    "hk_probe_broken.py": "raise RuntimeError('wheel built for another ABI')\n",
}


class _FakeModules:
    def _install_fake_modules(self):
        self.mod_dir = tempfile.mkdtemp()
        for name, body in _FAKE_MODULES.items():
            with open(os.path.join(self.mod_dir, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        sys.path.insert(0, self.mod_dir)
        self.addCleanup(self._remove_fake_modules)

    def _remove_fake_modules(self):
        sys.path.remove(self.mod_dir)
        for name in list(sys.modules):
            if name.startswith("hk_probe_"):
                del sys.modules[name]
        shutil.rmtree(self.mod_dir, ignore_errors=True)


class DependencyProbeTests(_FakeModules, unittest.TestCase):
    def setUp(self):
        self._install_fake_modules()

    def test_every_failure_is_listed_with_its_cause_never_raised(self):
        with self.assertLogs("sa02m_homekit", "ERROR") as logs:
            missing = main.missing_dependencies(
                ("hk_probe_ok", "hk_probe_subdep", "hk_probe_broken", "hk_probe_absent_zz9"))
        self.assertEqual(missing, [
            "hk_probe_subdep (needs hk_probe_absent_leaf_zz9)",
            "hk_probe_broken (RuntimeError)",
            "hk_probe_absent_zz9",
        ])
        self.assertEqual(len(logs.records), 3)

    def test_the_probe_covers_the_bridge_import_surface(self):
        probed = set(main.REQUIRED_MODULES)
        for name in ("_cffi_backend", "zeroconf", "pyhap.accessory_driver", "segno",
                     "paho.mqtt.client", "sa02m_alice.client.device_registry", "sa02m_homekit.bridge"):
            self.assertIn(name, probed)
        roots = {m.split(".")[0] for m in probed}
        with open(os.path.join(os.path.dirname(main.__file__), "bridge.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        third_party = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                third_party.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                third_party.update(a.name.split(".")[0] for a in node.names)
        third_party -= set(sys.stdlib_module_names) | {"__future__"}
        self.assertTrue(third_party, "bridge.py parse found no third-party import")
        self.assertLessEqual(third_party, roots)


class DependencyStandbyTests(_FakeModules, DaemonCase):
    def setUp(self):
        super().setUp()
        self._install_fake_modules()

    def test_a_missing_sub_dependency_is_missing_deps_not_a_crash(self):
        self.h.conf(enabled=True)
        self.h.daemon._check_dependencies = lambda: main.missing_dependencies(
            ("hk_probe_ok", "hk_probe_subdep"))
        with self.assertLogs("sa02m_homekit", "ERROR"):
            self.h.start()
            self.assertEqual(self.h.join(), main.EXIT_OK)
        st = self.h.status_json()
        self.assertEqual(st["state"], C.STATE_MISSING_DEPS)
        self.assertIn("hk_probe_absent_leaf_zz9", st["message"])
        self.assertEqual(self.h.runners, [])
        self.assertIsNone(self.h.mqtt)


class ListenerTests(DaemonCase):
    def test_no_address_means_no_interface_and_nothing_listens(self):
        self.h.conf(enabled=True)
        self.h.address = None
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_NO_INTERFACE))
        self.assertEqual(self.h.runners, [])
        self.assertFalse(self.h.setup_exists())
        # DHCP arrives: the next address poll starts the listener
        self.h.address = "192.168.1.50"
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.assertEqual(self.h.runners[-1].kw["address"], "192.168.1.50")

    def test_port_in_use(self):
        self.h.conf(enabled=True, port=30000)
        self.h.start_error = netif.PortInUse(98, "in use")
        with self.assertLogs("sa02m_homekit", "ERROR"):
            self.h.start()
            self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_PORT_IN_USE))
        self.assertFalse(self.h.setup_exists())
        self.h.start_error = None   # the other service went away
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))

    def test_binds_the_chosen_interface_address_and_names_the_bridge(self):
        self.h.conf(enabled=True, interface="eth1", port=30001)
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        kw = self.h.runners[0].kw
        self.assertEqual((kw["address"], kw["port"], kw["bridge_name"]),
                         ("192.168.1.136", 30001, "SA-02m 110002"))
        self.assertEqual(kw["persist_file"], C.STATE_FILE)
        st = self.h.status_json()
        self.assertEqual((st["interface"], st["address"], st["port"]), ("eth1", "192.168.1.136", 30001))

    def test_address_change_rebuilds_on_the_new_address(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.h.address = "10.0.0.7"
        self.assertTrue(self.h.wait_for(lambda: self.h.runners[-1].kw["address"] == "10.0.0.7"))
        self.assertTrue(self.h.runners[0].stopped)


class InterfaceChoiceTests(DaemonCase):
    """D3: the daemon asks for the address of the ONE configured wired port —
    never another interface, never a modem, even when the conf names one."""

    def _record(self):
        asked = []

        def address_of(name):
            asked.append(name)
            return "192.168.1.136"

        self.h.daemon._address_of = address_of
        return asked

    def test_only_the_chosen_interface_is_asked(self):
        asked = self._record()
        self.h.conf(enabled=True, interface="eth1")
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.assertTrue(asked)
        self.assertEqual(set(asked), {"eth1"})

    def test_a_hand_edited_modem_name_falls_back_to_eth0(self):
        asked = self._record()
        with open(self.h.conf_path, "w", encoding="utf-8") as fh:
            fh.write("[bridge]\nenabled = true\ninterface = ppp0\nport = 21064\n")
        with self.assertLogs("sa02m_homekit", "WARNING"):
            self.h.start()
            self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.assertEqual(set(asked), {"eth0"})
        self.assertEqual(self.h.status_json()["interface"], "eth0")


class SetupLifetimeTests(DaemonCase):
    def test_setup_exists_only_while_running_and_unpaired(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(self.h.setup_exists))
        runner = self.h.runners[0]
        setup = self.h.setup_json()
        self.assertEqual(setup["code"], runner.pincode)
        self.assertTrue(setup["setup_uri"].endswith(runner.setup_id))
        self.assertEqual(self.h.status_json()["paired"], False)
        runner.pair()
        self.assertTrue(self.h.wait_for(lambda: not self.h.setup_exists()))
        st = self.h.status_json()
        self.assertEqual((st["paired"], st["pairings"]), (True, 1))
        self.assertEqual(self.h.finish(), 0)
        self.assertFalse(self.h.setup_exists())

    def test_stop_removes_setup_and_leaves_starting_while_enabled(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(self.h.setup_exists))
        self.assertEqual(self.h.finish(), 0)
        self.assertFalse(self.h.setup_exists())
        self.assertEqual(self.h.state(), C.STATE_STARTING)
        self.assertTrue(self.h.runners[0].stopped)
        self.assertTrue(self.h.mqtt.stopped)

    def test_disabling_the_conf_stops_and_exits(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(self.h.setup_exists))
        self.h.conf(enabled=False)
        self.assertEqual(self.h.join(), 0)
        self.assertEqual(self.h.state(), C.STATE_DISABLED)
        self.assertFalse(self.h.setup_exists())
        self.assertTrue(self.h.runners[0].stopped)

    def test_setup_code_never_lands_in_status(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(self.h.setup_exists))
        code = self.h.runners[0].pincode
        with open(self.h.status.status_path, encoding="utf-8") as fh:
            self.assertNotIn(code, fh.read())


class RebuildTests(DaemonCase):
    def test_projection_change_rebuilds_on_the_same_store_carrying_the_lockout(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        first = self.h.runners[0]
        first.pair_setup_failures = 7
        self.h.doc["devices"][0]["name"] = "Розетка"
        self.h.doc_changed = True
        self.assertTrue(self.h.wait_for(lambda: len(self.h.runners) == 2 and self.h.runners[1].running))
        second = self.h.runners[1]
        self.assertTrue(first.stopped)
        self.assertEqual(second.kw["persist_file"], first.kw["persist_file"])
        self.assertEqual(second.kw["pair_setup_failures"], 7)
        self.assertEqual([s.name for s in second.kw["specs"]], ["Розетка"])
        self.assertEqual([s.aid for s in second.kw["specs"]], [s.aid for s in first.kw["specs"]])

    def test_value_only_document_change_does_not_rebuild(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.h.doc["devices"][0]["capabilities"][0]["mqtt"] = "/devices/mr02m-COM3-10/controls/do_2"
        self.h.doc_changed = True
        self.assertTrue(self.h.wait_for(
            lambda: "/devices/mr02m-COM3-10/controls/do_2" in self.h.mqtt.topics))
        self.assertEqual(len(self.h.runners), 1)

    def test_mqtt_values_reach_the_runner(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.h.mqtt.on_message("/devices/mr02m-COM3-10/controls/do_1", "1", False)
        self.assertTrue(self.h.wait_for(lambda: ("relay", [(0, "On", True), (0, "OutletInUse", True)], True)
                                        in self.h.runners[0].pushed))


class IdentityReasonTests(DaemonCase):
    def test_regenerated_identity_reason(self):
        with open(C.STATE_FILE, "w") as fh:
            fh.write("{}")   # a store from another board (no binding recorded)
        self.h.conf(enabled=True)
        with self.assertLogs("sa02m_homekit.identity", "WARNING"):
            self.h.start()
            self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.assertEqual(self.h.status_json()["reason"], C.REASON_IDENTITY_REGENERATED)

    def test_corrupt_store_reason_until_paired(self):
        identity.check_machine_binding()   # this board is the bound one
        with open(C.STATE_FILE, "w") as fh:
            fh.write("{torn")
        self.h.conf(enabled=True)
        with self.assertLogs("sa02m_homekit.identity", "ERROR"):
            self.h.start()
            self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.assertEqual(self.h.status_json()["reason"], C.REASON_STATE_CORRUPT)
        self.h.runners[0].pair()
        self.assertTrue(self.h.wait_for(lambda: self.h.status_json()["reason"] == ""))

    def test_locked_pair_setup_is_reported(self):
        self.h.conf(enabled=True)
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        runner = self.h.runners[0]
        runner.pair_setup_locked = True
        runner.kw["on_pairing_changed"]()
        self.assertTrue(self.h.wait_for(
            lambda: self.h.status_json()["reason"] == C.REASON_PAIR_SETUP_LOCKED))
        self.assertTrue(self.h.status_json()["pair_setup_locked"])


if __name__ == "__main__":
    unittest.main()
