"""bridge.py — the pyhap glue. DEPS-GUARDED: skips loudly without HAP-python
(reported as SKIP, never PASS); REAL in the venv / CI.

Nothing here binds a socket or starts zeroconf: the driver is built (store
loaded or written) but never started, and the HAP handlers are driven
directly. Proves: accessories per spec, one listen address, `available`
honoured on reads, a refused write answers -70402, the fsync'd `.hk-` persist
that our own store check accepts, a rebuild that keeps pairings / code /
keys, and the pair-setup attempt lockout.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
import uuid
from unittest import mock

from sa02m_homekit import constants as C
from sa02m_homekit import fsutil, identity
from sa02m_homekit import projection as P

from . import _deps

CODE = "031-45-154"
SETUP_ID = "7OSX"
ADDRESS = "192.0.2.10"
PORT = 21064
CLIENT = ("192.0.2.5", 50000)

DEVICES = (
    ({"id": "relay", "name": "Реле", "type": "devices.types.socket"},
     [{"type": P.CAP_ON_OFF, "mqtt": "/devices/m/controls/do_1", "parameters": {"instance": "on"}}], []),
    ({"id": "lamp", "name": "Лампа", "type": "devices.types.light"},
     [{"type": P.CAP_ON_OFF, "mqtt": "/devices/m/controls/do_2", "parameters": {"instance": "on"}},
      {"type": P.CAP_RANGE, "mqtt": "/devices/m/controls/dim",
       "parameters": {"instance": "brightness", "range": {"min": 0, "max": 255, "precision": 1}}}], []),
    ({"id": "room", "name": "Комната", "type": "devices.types.sensor"},
     [], [{"type": P.PROP_FLOAT, "mqtt": "/devices/d/controls/t",
           "parameters": {"instance": "temperature", "unit": P.UNIT_CELSIUS}}]),
    ({"id": "door", "name": "Дверь", "type": "devices.types.switch"},
     [{"type": P.CAP_ON_OFF, "mqtt": "/devices/m/controls/di_1", "writable": False,
       "parameters": {"instance": "on"}}], []),
)


def make_specs(names=None):
    names = names or {}
    specs = []
    for aid, (device, caps, props) in enumerate(DEVICES, start=2):
        services, _skipped = P.device_services(device, caps, props)
        specs.append(P.AccessorySpec(
            device_id=device["id"], name=names.get(device["id"], device["name"]),
            model=device["type"], services=services, aid=aid))
    return specs


class FakeVerifier:
    """An SRP verifier whose proof check is scripted (no 3072-bit math)."""

    def __init__(self, result):
        self.result = result

    def set_A(self, _a):
        pass

    def verify(self, _m):
        return self.result


@unittest.skipUnless(_deps.HAVE_PYHAP, _deps.PYHAP_SKIP)
class GlueCase(unittest.TestCase):
    def setUp(self):
        from pyhap import tlv
        from pyhap.const import HAP_SERVER_STATUS
        from pyhap.hap_handler import HAP_TLV_ERRORS, HAP_TLV_STATES, HAP_TLV_TAGS

        from sa02m_homekit import bridge
        from sa02m_homekit.engine import WriteRefused

        self.bridge = bridge
        self.tlv = tlv
        self.status = HAP_SERVER_STATUS
        self.errors, self.states, self.tags = HAP_TLV_ERRORS, HAP_TLV_STATES, HAP_TLV_TAGS
        self.WriteRefused = WriteRefused
        self.dir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.dir, "state.json")
        self.drivers = []
        self.writes = []
        self.refuse = False
        self.changed = 0

    def tearDown(self):
        for driver in self.drivers:
            if driver.executor is not None:
                driver.executor.shutdown(wait=False)
            if not driver.loop.is_closed():
                driver.loop.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, device_id, binding, value):
        if self.refuse:
            raise self.WriteRefused("%s: broker not connected" % device_id)
        self.writes.append((device_id, binding.char, value))

    def _on_changed(self):
        self.changed += 1

    def build(self, *, specs=None, code=CODE, setup_id=SETUP_ID, failures=0):
        runner = self.bridge.BridgeRunner(
            persist_file=self.state_path, address=ADDRESS, port=PORT,
            bridge_name="SA-02m 110002", specs=make_specs() if specs is None else specs,
            write_cb=self._write, firmware="1.0.6", new_code=code, new_setup_id=setup_id,
            on_pairing_changed=self._on_changed, pair_setup_failures=failures)
        runner.driver = runner._build()
        self.drivers.append(runner.driver)
        return runner

    def driver(self, **kw):
        kw.setdefault("on_pairing_changed", self._on_changed)
        drv = self.bridge.SafeDriver(address=ADDRESS, port=PORT, persist_file=self.state_path,
                                     pincode=CODE.encode("ascii"), **kw)
        self.drivers.append(drv)
        return drv

    @staticmethod
    def iid(acc, index, name):
        return acc.iid_manager.get_iid(acc.chars[(index, name)][0])


class AccessoryTests(GlueCase):
    def test_bridge_and_accessories_follow_the_specs(self):
        from pyhap.const import CATEGORY_OUTLET, CATEGORY_SENSOR

        runner = self.build()
        br = runner.driver.accessory
        self.assertIsInstance(br, self.bridge.SafeBridge)
        self.assertEqual(br.aid, 1)
        self.assertEqual(sorted(br.accessories), [2, 3, 4, 5])
        relay = runner.accessories["relay"]
        self.assertEqual((relay.aid, relay.display_name, relay.category), (2, "Реле", CATEGORY_OUTLET))
        outlet = {c.display_name for c in relay.get_service("Outlet").characteristics}
        self.assertTrue({"On", "OutletInUse"} <= outlet, outlet)
        info = relay.get_service("AccessoryInformation")
        self.assertEqual(
            [info.get_characteristic(n).get_value()
             for n in ("Manufacturer", "Model", "SerialNumber", "FirmwareRevision")],
            ["CYNTRON", "devices.types.socket", "relay", "1.0.6"])
        self.assertIsNotNone(runner.accessories["lamp"].get_service("Lightbulb")
                             .get_characteristic("Brightness"))
        room = runner.accessories["room"]
        self.assertEqual(room.category, CATEGORY_SENSOR)
        props = room.chars[(0, "CurrentTemperature")][0].properties
        self.assertEqual((props["minValue"], props["maxValue"], props["minStep"]), (-100, 200, 0.1))

    def test_only_writable_bindings_carry_a_setter(self):
        runner = self.build()
        relay, door = runner.accessories["relay"], runner.accessories["door"]
        self.assertIsNotNone(relay.chars[(0, "On")][0].setter_callback)
        self.assertIsNone(relay.chars[(0, "OutletInUse")][0].setter_callback)
        self.assertIsNone(door.chars[(0, "ContactSensorState")][0].setter_callback)

    def test_listener_is_bound_to_the_one_chosen_address(self):
        driver = self.build().driver
        self.assertEqual(driver.state.addresses, [ADDRESS])            # advertised
        self.assertIsInstance(driver.http_server, self.bridge.SafeHAPServer)
        self.assertEqual(driver.http_server._addr_port, (ADDRESS, PORT))  # listen
        self.assertEqual(driver.interface_choice, [ADDRESS])           # mDNS interface
        self.assertNotIn("0.0.0.0", json.dumps([driver.state.addresses, driver.interface_choice]))


class ReadWriteTests(GlueCase):
    def _read(self, driver, aid, iid):
        return driver.get_characteristics(["%d.%d" % (aid, iid)])["characteristics"][0]

    def test_reads_honour_availability(self):
        runner = self.build()
        relay = runner.accessories["relay"]
        iid = self.iid(relay, 0, "On")
        first = self._read(runner.driver, relay.aid, iid)
        self.assertEqual(first["status"], self.status.SERVICE_COMMUNICATION_FAILURE)  # never seen
        self.assertNotIn("value", first)
        relay.apply([(0, "On", True), (0, "OutletInUse", True)], True)
        live = self._read(runner.driver, relay.aid, iid)
        self.assertEqual((live["status"], live["value"]), (self.status.SUCCESS, True))
        relay.apply([], False)    # module offline: «Не отвечает», not the stale True
        gone = self._read(runner.driver, relay.aid, iid)
        self.assertEqual(gone["status"], self.status.SERVICE_COMMUNICATION_FAILURE)
        self.assertNotIn("value", gone)

    def test_a_write_reaches_the_callback(self):
        runner = self.build()
        relay, lamp = runner.accessories["relay"], runner.accessories["lamp"]
        query = {"characteristics": [
            {"aid": relay.aid, "iid": self.iid(relay, 0, "On"), "value": True},
            {"aid": lamp.aid, "iid": self.iid(lamp, 0, "Brightness"), "value": 50},
        ]}
        self.assertIsNone(runner.driver.set_characteristics(query, CLIENT))  # all succeeded
        self.assertEqual(self.writes, [("relay", "On", True), ("lamp", "Brightness", 50)])

    def test_a_refused_write_answers_service_communication_failure(self):
        runner = self.build()
        relay = runner.accessories["relay"]
        self.refuse = True
        query = {"characteristics": [{"aid": relay.aid, "iid": self.iid(relay, 0, "On"), "value": True}]}
        with self.assertLogs("pyhap.accessory_driver", "ERROR"):
            resp = runner.driver.set_characteristics(query, CLIENT)
        self.assertEqual(resp["characteristics"][0]["status"],
                         self.status.SERVICE_COMMUNICATION_FAILURE)
        self.assertEqual(self.status.SERVICE_COMMUNICATION_FAILURE, -70402)


PHASE3_DEVICES = (
    ({"id": "air", "name": "Воздух", "type": "devices.types.sensor.climate"},
     [], [{"type": P.PROP_FLOAT, "mqtt": "/devices/d/controls/co2",
           "parameters": {"instance": "co2_level", "unit": "unit.ppm"}}]),
    ({"id": "wall", "name": "Кнопка", "type": "devices.types.sensor.button"},
     [], [{"type": P.PROP_EVENT, "mqtt": "/devices/mr02m-COM3-10/controls/di_3",
           "parameters": {"instance": "button",
                          "events": [{"value": "click"}, {"value": "long_press"}]}}]),
    ({"id": "th", "name": "Термостат", "type": "devices.types.thermostat"},
     [{"type": P.CAP_RANGE, "mqtt": "/devices/m/controls/ao_1",
       "parameters": {"instance": "temperature", "unit": P.UNIT_CELSIUS,
                      "range": {"min": 5, "max": 35, "precision": 0.5}}}],
     [{"type": P.PROP_FLOAT, "mqtt": "/devices/d/controls/t",
       "parameters": {"instance": "temperature", "unit": P.UNIT_CELSIUS}}]),
    ({"id": "scene-hk-s1", "name": "Вечер", "type": "devices.types.switch", "scene_id": "s1"},
     [{"type": P.CAP_ON_OFF, "mqtt": "/devices/sa02m-rules-s1/controls/run",
       "parameters": {"split": True}}], []),
)


def make_phase3_specs():
    specs = []
    for aid, (device, caps, props) in enumerate(PHASE3_DEVICES, start=10):
        services, skipped = P.device_services(device, caps, props)
        assert services and not skipped, (device["id"], skipped)
        specs.append(P.AccessorySpec(device_id=device["id"], name=device["name"],
                                     model=device["type"], services=services, aid=aid))
    return specs


class Phase3GlueTests(GlueCase):
    """Rows M15–M18 rendered into real pyhap objects."""

    def test_co2_sensor_carries_the_optional_level(self):
        acc = self.build(specs=make_phase3_specs()).accessories["air"]
        svc = acc.get_service("CarbonDioxideSensor")
        names = {c.display_name for c in svc.characteristics}
        self.assertTrue({"CarbonDioxideDetected", "CarbonDioxideLevel"} <= names, names)
        acc.apply([(0, "CarbonDioxideDetected", 1), (0, "CarbonDioxideLevel", 1234.0)], True)
        self.assertEqual(acc.chars[(0, "CarbonDioxideLevel")][0].get_value(), 1234.0)

    def test_button_valid_values_and_every_press_notifies(self):
        from pyhap.const import CATEGORY_PROGRAMMABLE_SWITCH

        runner = self.build(specs=make_phase3_specs())
        acc = runner.accessories["wall"]
        self.assertEqual(acc.category, CATEGORY_PROGRAMMABLE_SWITCH)
        char = acc.chars[(0, "ProgrammableSwitchEvent")][0]
        self.assertEqual(char.properties["ValidValues"], {"SinglePress": 0, "LongPress": 2})
        with mock.patch.object(runner.driver, "publish") as publish:
            acc.fire(0, "ProgrammableSwitchEvent", 0)
            acc.fire(0, "ProgrammableSwitchEvent", 0)     # the same press twice
            acc.fire(0, "ProgrammableSwitchEvent", 2)
        values = [c.args[0]["value"] for c in publish.call_args_list]
        self.assertEqual(values, [0, 0, 2])
        self.assertIsNone(char.get_value())                # always-null between events

    def test_thermostat_props_and_valid_values_reach_pyhap(self):
        from pyhap.const import CATEGORY_THERMOSTAT

        runner = self.build(specs=make_phase3_specs())
        acc = runner.accessories["th"]
        self.assertEqual(acc.category, CATEGORY_THERMOSTAT)
        target = acc.chars[(0, "TargetTemperature")][0]
        self.assertEqual((target.properties["minValue"], target.properties["maxValue"],
                          target.properties["minStep"]), (5, 35, 0.5))
        mode = acc.chars[(0, "TargetHeatingCoolingState")][0]
        self.assertEqual(mode.properties["ValidValues"], {"Heat": 1})
        self.assertIsNotNone(target.setter_callback)
        self.assertIsNone(mode.setter_callback)
        # A 5 °C setpoint is inside the dial (HAP's default floor is 10 °C).
        query = {"characteristics": [{"aid": acc.aid, "iid": self.iid(acc, 0, "TargetTemperature"),
                                      "value": 5}]}
        self.assertIsNone(runner.driver.set_characteristics(query, CLIENT))
        self.assertEqual(self.writes, [("th", "TargetTemperature", 5)])

    def test_scene_switch_resets_to_off_after_the_timer(self):
        import asyncio

        runner = self.build(specs=make_phase3_specs())
        acc = runner.accessories["scene-hk-s1"]
        char = acc.chars[(0, "On")][0]
        query = {"characteristics": [{"aid": acc.aid, "iid": self.iid(acc, 0, "On"), "value": True}]}
        with mock.patch.object(C, "SCENE_RESET_S", 0.05):
            self.assertIsNone(runner.driver.set_characteristics(query, CLIENT))
            self.assertEqual(self.writes, [("scene-hk-s1", "On", True)])
            self.assertIs(char.get_value(), True)
            runner.driver.loop.run_until_complete(asyncio.sleep(0.3))
        self.assertIs(char.get_value(), False)

    def test_a_refused_scene_run_is_reset_too(self):
        import asyncio

        runner = self.build(specs=make_phase3_specs())
        acc = runner.accessories["scene-hk-s1"]
        self.refuse = True
        query = {"characteristics": [{"aid": acc.aid, "iid": self.iid(acc, 0, "On"), "value": True}]}
        with mock.patch.object(C, "SCENE_RESET_S", 0.05), \
                self.assertLogs("pyhap.accessory_driver", "ERROR"):
            resp = runner.driver.set_characteristics(query, CLIENT)
            runner.driver.loop.run_until_complete(asyncio.sleep(0.3))
        self.assertEqual(resp["characteristics"][0]["status"], self.status.SERVICE_COMMUNICATION_FAILURE)
        self.assertIs(acc.chars[(0, "On")][0].get_value(), False)


class PersistTests(GlueCase):
    def test_persist_is_fsynced_0600_via_hk_tmp_and_passes_our_store_check(self):
        real_fsync, real_mkstemp = os.fsync, fsutil.tempfile.mkstemp
        fsyncs, prefixes = [], []

        def fsync(fd):
            fsyncs.append(fd)
            return real_fsync(fd)

        def mkstemp(*a, **kw):
            prefixes.append(kw.get("prefix"))
            return real_mkstemp(*a, **kw)

        with mock.patch.object(fsutil.os, "fsync", side_effect=fsync), \
                mock.patch.object(fsutil.tempfile, "mkstemp", side_effect=mkstemp):
            self.build()   # a fresh store is written by driver.add_accessory → persist
        self.assertGreaterEqual(len(fsyncs), 2)          # the file and its directory
        self.assertEqual(prefixes, [C.TMP_PREFIX])
        self.assertEqual(stat.S_IMODE(os.stat(self.state_path).st_mode), 0o600)
        self.assertEqual(os.listdir(self.dir), ["state.json"])
        with open(self.state_path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual((data["pincode"], data["setup_id"]), (CODE, SETUP_ID))
        self.assertIsNone(identity.check_state_file(self.state_path))
        self.assertTrue(os.path.exists(self.state_path))


class RebuildTests(GlueCase):
    def test_a_rebuild_keeps_pairings_code_and_keys(self):
        from cryptography.hazmat.primitives import serialization

        def pub(driver):
            return driver.state.public_key.public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)

        first = self.build()
        first.driver.state.add_paired_client(str(uuid.uuid4()).encode("utf-8"), b"\x01" * 32, b"\x01")
        first.driver.persist()
        second = self.build(specs=make_specs({"relay": "Розетка"}), code="482-19-305", setup_id="ZZZZ")
        self.assertTrue(second.paired)
        self.assertEqual(second.pairings, 1)
        self.assertEqual((second.pincode, second.setup_id), (CODE, SETUP_ID))  # the store wins
        self.assertEqual(second.driver.state.mac, first.driver.state.mac)
        self.assertEqual(pub(second.driver), pub(first.driver))
        self.assertEqual(second.accessories["relay"].display_name, "Розетка")
        self.assertEqual(second.accessories["relay"].aid, first.accessories["relay"].aid)

    def test_pairing_changes_are_signalled(self):
        runner = self.build()
        with mock.patch.object(runner.driver, "async_persist"):
            runner.driver.pair(str(uuid.uuid4()).encode("utf-8"), b"\x02" * 32, b"\x01")
        self.assertEqual(self.changed, 1)
        self.assertTrue(runner.paired)


class LockoutTests(GlueCase):
    def _handler(self, driver):
        handler = self.bridge.LockoutHandler(driver, CLIENT)
        handler.sent = []
        handler._send_tlv_pairing_response = handler.sent.append
        return handler

    def _m3(self):
        return {self.tags.PUBLIC_KEY: b"A", self.tags.PASSWORD_PROOF: b"M"}

    def test_wrong_codes_lock_pair_setup_until_restart(self):
        driver = self.driver(max_pair_setup_fails=3)
        handler = self._handler(driver)
        driver.srp_verifier = FakeVerifier(None)       # wrong setup code
        for _ in range(2):
            handler._pairing_two(self._m3())
        self.assertEqual(driver.pair_setup_failures, 2)
        self.assertFalse(driver.pair_setup_locked)
        self.assertEqual(self.tlv.decode(handler.sent[-1])[self.tags.ERROR_CODE],
                         self.errors.AUTHENTICATION)
        # a pair-verify M4 authentication error is not a setup-code guess
        handler._send_authentication_error_tlv_response(self.states.M4)
        # a correct proof is not a failure
        driver.srp_verifier = FakeVerifier(b"hamk")
        handler._pairing_two(self._m3())
        self.assertEqual(driver.pair_setup_failures, 2)
        driver.srp_verifier = FakeVerifier(None)
        with self.assertLogs("sa02m_homekit.bridge", "WARNING"):
            handler._pairing_two(self._m3())
        self.assertTrue(driver.pair_setup_locked)
        self.assertEqual(self.changed, 3)
        # every new connection is refused MaxTries before any SRP work
        fresh = self._handler(driver)
        fresh.request_body = self.tlv.encode(self.tags.SEQUENCE_NUM, self.states.M1)
        with mock.patch.object(driver, "setup_srp_verifier") as srp:
            fresh.handle_pairing()
        srp.assert_not_called()
        answer = self.tlv.decode(fresh.sent[-1])
        self.assertEqual((answer[self.tags.SEQUENCE_NUM], answer[self.tags.ERROR_CODE]),
                         (self.states.M2, self.bridge.TLV_ERROR_MAX_TRIES))

    def test_default_limit_is_the_hap_spec_100(self):
        self.assertEqual(C.PAIR_SETUP_MAX_FAILS, 100)
        self.assertEqual(self.driver()._max_pair_setup_fails, 100)

    def test_the_count_survives_an_in_process_rebuild(self):
        runner = self.build(failures=C.PAIR_SETUP_MAX_FAILS)
        self.assertTrue(runner.pair_setup_locked)
        self.assertEqual(runner.pair_setup_failures, C.PAIR_SETUP_MAX_FAILS)

    def test_every_connection_gets_the_lockout_handler(self):
        runner = self.build()
        proto = self.bridge.SafeHAPProtocol(runner.driver.loop, {}, runner.driver)
        transport = mock.Mock()
        transport.get_extra_info.return_value = CLIENT
        proto.connection_made(transport)
        self.assertIsInstance(proto.handler, self.bridge.LockoutHandler)


def setUpModule():
    if not _deps.HAVE_PYHAP:
        _deps.announce(_deps.PYHAP_SKIP)


if __name__ == "__main__":
    unittest.main()
