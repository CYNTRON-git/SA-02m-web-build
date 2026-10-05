"""SPODES poller: live cache, no Modbus port, disconnect stays off the wire."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(BRIDGE_DIR.parent / "sa02m-spodes"))

from sa02m_spodes.axdr import i8, structure, u32, u8, array  # noqa: E402
from sa02m_spodes.xdlms import CosemAccessError  # noqa: E402

import bridge_spodes  # noqa: E402
import sa02m_spodes.link as spodes_link  # noqa: E402
from bridge_device import HdlcPortScheduler  # noqa: E402
from bridge_spodes import SpodesPoller  # noqa: E402
from modbus_mqtt_bridge import make_port_scheduler  # noqa: E402

_VOLTAGE = bytes((1, 0, 32, 7, 0, 255))


class Pub:
    def __init__(self):
        self.controls = {}
        self.errors = {}
        self.online = []
        self.meta = {}

    def pub_control(self, did, name, value, **_k):
        self.controls[(did, name)] = value

    def pub_control_units(self, *_a, **_k):
        pass

    def pub_control_meta(self, *_a, **_k):
        pass

    def pub_meta(self, did, key, value):
        self.meta[(did, key)] = value

    def pub_error(self, did, name, code):
        self.errors[(did, name)] = code

    def device_online(self, did, online):
        self.online.append((did, online))

    def subscribe_writeback(self, *_a, **_k):
        pass

    def register_device(self, *_a, **_k):
        pass


class Meter:
    def __init__(self):
        self.associated = True
        self.gets = []
        self.actions = []
        self.fail_action = False

    def get(self, class_id, obis, attr, access=None, timeout_s=None):
        self.gets.append((class_id, bytes(obis), attr, timeout_s))
        if class_id == 7:
            return array([])
        if class_id == 70 and attr == 2:
            return u8(1)
        if bytes(obis) == _VOLTAGE and attr == 2:
            return u32(2300)
        if attr == 3:
            return structure([i8(-1), u8(35)])
        raise CosemAccessError(9)

    def action(self, class_id, obis, method, param=None, timeout_s=None):
        if self.fail_action:
            raise IOError("meter refused")
        self.actions.append((class_id, method))

    def release(self):
        self.associated = False

    def associate(self):
        self.associated = True


def _cfg(**extra):
    cfg = {
        "id": "spodes-COM2-17",
        "type": "spodes",
        "port": "/dev/COM2",
        "baudrate": 9600,
        "hdlc_address": 17,
        "association": "public",
        "password": "secret-lls",
        "poll_power_s": 0,
        "offline_after_fails": 2,
    }
    cfg.update(extra)
    return cfg


class PollerTest(unittest.TestCase):
    def test_voltage_is_published_and_the_port_is_not_modbus(self):
        pub = Pub()
        cfg = _cfg()
        poller = SpodesPoller(cfg, pub)
        poller._session = Meter()
        self.assertEqual(poller.bus.transport, "hdlc")
        self.assertEqual(poller.bus.baudrate, 9600)
        with self.assertRaises(RuntimeError):
            poller.get_port()
        poller.poll_io()
        self.assertEqual(pub.controls[("spodes-COM2-17", "voltage_a")], "230")
        self.assertFalse(any(g[0] == 7 for g in poller._session.gets))
        self.assertNotIn("secret-lls", repr(poller.cfg))
        self.assertEqual(cfg["password"], "secret-lls")

    def test_offline_after_repeated_failures(self):
        pub = Pub()
        poller = SpodesPoller(_cfg(), pub)
        dead = Meter()
        dead.get = lambda *a, **k: (_ for _ in ()).throw(IOError("down"))
        poller._session = dead

        def _drop():
            SpodesPoller._drop(poller)
            dead.associated = True
            poller._session = dead

        poller._drop = _drop
        poller.poll_io()
        self.assertEqual(pub.online, [])
        poller._t_power = 0.0
        poller.poll_io()
        self.assertEqual(pub.online, [("spodes-COM2-17", False)])

    def test_scheduler_does_not_open_a_modbus_port(self):
        poller = SpodesPoller(_cfg(), Pub())

        def boom():
            raise AssertionError("ModbusSerial")

        poller.get_port = boom
        sched, _name = make_port_scheduler(poller.bus.key, [poller], {})
        self.assertIsInstance(sched, HdlcPortScheduler)

    def test_public_disconnect_sends_no_action(self):
        pub = Pub()
        poller = SpodesPoller(_cfg(), pub)
        wire = []
        poller._transport = type("L", (), {"exchange": lambda *a, **k: wire.append(a)})()
        poller.load_command("1")
        self.assertEqual(wire, [])
        self.assertEqual(poller.action_calls, 0)
        self.assertEqual(pub.errors[("spodes-COM2-17", "load_disconnect")], "w")

    def test_configurator_action_and_a_failed_action_does_not_paint_state(self):
        pub = Pub()
        poller = SpodesPoller(_cfg(association="configurator"), pub)
        poller.role = "configurator"
        meter = Meter()
        poller._session = meter
        poller.load_command("1")
        self.assertEqual(poller.action_calls, 1)
        self.assertEqual(pub.controls[("spodes-COM2-17", "load_state")], "1")
        meter.fail_action = True
        poller.load_command("0")
        self.assertEqual(poller.action_calls, 1)
        self.assertEqual(pub.errors[("spodes-COM2-17", "load_disconnect")], "w")

    def test_reader_without_keys_stays_public(self):
        pub = Pub()
        poller = SpodesPoller(_cfg(association="reader"), pub)
        self.assertEqual(poller.role, "public")
        self.assertIsNone(poller._security)
        self.assertEqual(poller._sap, 16)
        poller.setup()
        self.assertEqual(pub.controls[("spodes-COM2-17", "association")], "public")
        key = "00112233445566778899aabbccddeeff"
        armed = SpodesPoller(_cfg(
            association="reader", encryption_key=key,
            authentication_key=key, system_title="4d45524330303120"), Pub())
        self.assertEqual(armed.role, "reader")
        self.assertIsNotNone(armed._security)

    def test_profile_is_a_file_not_a_live_poll(self):
        pub = Pub()
        poller = SpodesPoller(_cfg(poll_profile_s=1, poll_energy_s=10**9), pub)
        meter = Meter()
        poller._session = meter
        with tempfile.TemporaryDirectory() as tmp:
            bridge_spodes.LIVE_CACHE_DIR = Path(tmp)
            poller.poll_io()
            self.assertFalse(any(g[0] == 7 for g in meter.gets))
            poller._t_profile = 0.0
            poller.poll_slow_if_due(10**9)
            budgets = [g[3] for g in meter.gets if g[0] == 7]
            self.assertTrue(budgets)
            self.assertTrue(all(b is not None and b <= 2.0 for b in budgets))
            self.assertTrue((Path(tmp) / "spodes-COM2-17.profile.json").is_file())
            self.assertFalse(any(name.startswith("profile") for _id, name in pub.controls))

    def test_transparent_shares_one_socket_and_stays_hdlc(self):
        cfg = {
            "id": "spodes-tcp-192_168_1_50-17", "type": "spodes",
            "transport": "transparent", "host": "192.168.1.50",
            "tcp_port": 4002, "hdlc_address": 17, "association": "public",
        }
        first = SpodesPoller(cfg, Pub())
        second = SpodesPoller(
            dict(cfg, id="spodes-tcp-192_168_1_50-18", hdlc_address=18), Pub())
        created = []

        class _FakeLink:
            dead = False

            def __init__(self, host, port, timeout_s=3.0):
                created.append((host, int(port)))

        bridge_spodes._GATEWAY_LINKS.clear()
        try:
            with patch.object(spodes_link, "TransparentLink", _FakeLink):
                self.assertIs(first._connect(), second._connect())
            self.assertEqual(created, [("192.168.1.50", 4002)])
            sched, _name = make_port_scheduler(first.bus.key, [first, second], {})
            self.assertIsInstance(sched, HdlcPortScheduler)
            with patch.object(bridge_spodes, "ClientSession") as sess:
                sess.return_value.associated = True
                first._open()
            self.assertEqual(sess.call_args.kwargs["mode"], "hdlc")
        finally:
            bridge_spodes._GATEWAY_LINKS.clear()


if __name__ == "__main__":
    unittest.main()
