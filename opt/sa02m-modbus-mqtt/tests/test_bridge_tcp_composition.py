#!/usr/bin/env python3
"""Composition of TCP devices into the bridge (compose_pollers) + TemplatePoller
end-to-end over a real socket.

Pins G-TCP (docs/contracts/bridge-modbus-tcp.md): a TCP device gets its own bus,
never Fast Modbus, never a COM port, never a roster row; an invalid TCP entry is
skipped loudly while the RS-485 fleet still registers; an RTU-only config
composes exactly as before; and the SAME scenario over a FakeSerial and over
TCP yields the SAME MQTT publish sequence (values, writeback echo, offline and
recovery).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _stub_missing(name: str, module: types.ModuleType) -> None:
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = module


_stub_missing("yaml", types.ModuleType("yaml"))
_stub_missing("serial", types.ModuleType("serial"))
try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import modbus_mqtt_bridge as bridge  # noqa: E402
import bridge_serial  # noqa: E402
import bridge_tcp  # noqa: E402
import bridge_template  # noqa: E402
from mbap_fake import Bank, FakeMbapServer  # noqa: E402

TEMPLATE = {"device": {"name": "TCP test AHU", "channels": [
    {"name": "supply_temp", "reg_type": "input", "address": 4000,
     "format": "s16", "scale": 0.1, "units": "deg C"},
    {"name": "run", "reg_type": "coil", "address": 16, "readonly": False},
    {"name": "temp_setpoint", "reg_type": "holding", "address": 190,
     "scale": 0.1, "readonly": False},
    {"name": "alarm", "reg_type": "discrete", "address": 9},
]}}


def _tcp_template(**kw):
    base = {"id": "tst-tcp-192_0_2_10-1", "type": "template", "template": "tcpt",
            "transport": "tcp", "host": "192.0.2.10", "tcp_port": 502,
            "address": 1}
    base.update(kw)
    return base


class _Isolated(unittest.TestCase):
    """Fresh module state: the poller list, the TCP pool, the writeback workers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "config-tcpt.json").write_text(
            json.dumps(TEMPLATE), encoding="utf-8")
        for patcher in (
            mock.patch.dict("os.environ", {"SA02M_WB_TEMPLATES_DIR": self.tmp.name}),
            mock.patch.object(bridge, "_pollers", []),
            mock.patch.dict(bridge_tcp._tcp_pool, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pub = mock.Mock()


class TestCompose(_Isolated):
    def _compose(self, devices):
        with mock.patch.object(bridge_serial, "get_port",
                               side_effect=AssertionError("serial port opened")):
            return bridge.compose_pollers(devices, self.pub)

    def test_rtu_only_config_composes_as_before(self):
        devices = [
            {"id": "mr-COM1-5", "type": "mr02m", "port": "/dev/COM1",
             "baudrate": 115200, "address": 5, "module_type": 1},
            {"id": "carel-COM3-1", "type": "carel", "port": "/dev/COM3",
             "baudrate": 19200, "address": 1},
            {"id": "dflt", "type": "template", "template": "tcpt", "address": 7},
            {"id": "carel-COM3-2", "type": "carel", "port": "/dev/COM3",
             "baudrate": 19200, "address": 2, "family": "uaria"},
        ]
        by_bus, _fmb, refused = self._compose(devices)
        self.assertEqual(refused, [])
        # Golden: today's keys, in today's (config) order.
        self.assertEqual(list(by_bus), ["/dev/COM1:115200", "/dev/COM3:19200"])
        self.assertEqual([p.device_id for p in by_bus["/dev/COM3:19200"]],
                         ["carel-COM3-1", "carel-COM3-2"])
        self.assertEqual([p.device_id for p in by_bus["/dev/COM1:115200"]],
                         ["mr-COM1-5", "dflt"])
        self.assertEqual([p.device_id for p in bridge._pollers],
                         ["mr-COM1-5", "carel-COM3-1", "dflt", "carel-COM3-2"])

    def test_a_tcp_device_is_its_own_bus_without_fmb(self):
        devices = [_tcp_template(fast_modbus=True),
                   {"id": "mr-COM1-5", "type": "mr02m", "port": "/dev/COM1",
                    "address": 5, "module_type": 1}]
        # Template/Carel declare no event ranges today, so give this one some:
        # the guard must hold on the transport, not on that coincidence.
        with mock.patch.object(bridge_template.TemplatePoller, "fmb_event_ranges",
                               return_value=[(bridge.FMB_EVT_INPUT, 0, 1)]):
            by_bus, fmb, _refused = self._compose(devices)
        self.assertEqual(list(by_bus), ["tcp:192.0.2.10:502", "/dev/COM1:115200"])
        self.assertNotIn("tcp:192.0.2.10:502", fmb)
        self.assertIn("/dev/COM1:115200", fmb)       # MR default ON is untouched
        p = by_bus["tcp:192.0.2.10:502"][0]
        self.assertIsNone(p.port_path)
        self.assertEqual(p.baudrate, 0)

    def test_two_endpoints_on_one_host_are_not_a_baud_conflict(self):
        devices = [_tcp_template(id="a", tcp_port=502),
                   _tcp_template(id="b", tcp_port=503)]
        with self.assertNoLogs("bridge", level="ERROR"):
            by_bus, _fmb, _r = self._compose(devices)
        self.assertEqual(len(by_bus), 2)

    def test_a_real_rtu_baud_conflict_is_still_reported(self):
        devices = [{"id": "a", "type": "mr02m", "port": "/dev/COM2", "baudrate": 9600},
                   {"id": "b", "type": "mr02m", "port": "/dev/COM2", "baudrate": 19200},
                   _tcp_template(id="c")]
        with self.assertLogs("bridge", level="ERROR") as logs:
            self._compose(devices)
        self.assertEqual(len([ln for ln in logs.output if "several baud" in ln]), 1)

    def test_a_refused_tcp_entry_is_skipped_loudly_and_rtu_still_registers(self):
        devices = [_tcp_template(id="self", host="127.0.0.1"),
                   {"id": "mr-COM1-5", "type": "mr02m", "port": "/dev/COM1",
                    "address": 5, "module_type": 1},
                   _tcp_template(id="mr-tcp", type="mr02m")]
        with self.assertLogs("bridge", level="ERROR") as logs:
            by_bus, _fmb, refused = self._compose(devices)
        self.assertEqual([r["reason"] for r in refused],
                         ["host_forbidden", "type_not_tcp_capable"])
        self.assertIn("device self refused: host_forbidden", "\n".join(logs.output))
        self.assertEqual([p.device_id for p in bridge._pollers], ["mr-COM1-5"])
        registered = [c.args[0] for c in self.pub.register_device.call_args_list]
        self.assertEqual(registered, ["mr-COM1-5"])

    def test_endpoint_17_is_refused(self):
        devices = [_tcp_template(id="d%d" % i, host="10.1.0.%d" % (i + 1))
                   for i in range(17)]
        by_bus, _fmb, refused = self._compose(devices)
        self.assertEqual(len(by_bus), 16)
        self.assertEqual(refused, [{"index": 16, "id": "d16",
                                    "reason": "tcp_endpoint_limit"}])

    def test_a_tcp_poller_never_reaches_the_serial_pool(self):
        by_bus, _f, _r = self._compose([_tcp_template()])
        p = by_bus["tcp:192.0.2.10:502"][0]
        with mock.patch.object(bridge_serial, "get_port",
                               side_effect=AssertionError("serial port opened")):
            client = p.get_port()
        self.assertIsInstance(client, bridge_tcp.ModbusTcpClient)
        self.assertEqual(client.label, "192.0.2.10:502")


class TestRoster(_Isolated):
    def test_no_tcp_row_and_no_ghost_port(self):
        devices = [
            {"id": "mr-COM1-5", "type": "mr02m", "port": "/dev/COM1", "address": 5},
            _tcp_template(),
            {"id": "carel-tcp-192_0_2_20-1", "type": "carel", "family": "crst",
             "transport": "tcp", "host": "192.0.2.20", "address": 1},
            {"id": "carel-COM3-1", "type": "carel", "port": "/dev/COM3", "address": 1},
        ]
        self.pub.device_online_snapshot.return_value = {}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "_roster.json"
            with mock.patch.object(bridge, "LIVE_CACHE_DIR", Path(d)):
                bridge.write_bridge_roster(devices, self.pub, path=path)
            rows = json.loads(path.read_text(encoding="utf-8"))["devices"]
        self.assertEqual([(r["port"], r["addr"], r["type"]) for r in rows],
                         [("COM1", 5, "mr02m"), ("COM3", 1, "carel")])


class _Rtu:
    """FakeSerial over the same Bank the MBAP server uses; `down` = no answer."""

    def __init__(self, bank):
        self.bank = bank
        self.down = False
        self.writes = []

    def _chk(self):
        if self.down:
            raise IOError("Short response: 0/7 bytes")

    def read_input_registers(self, a, start, n):
        self._chk()
        return [self.bank.regs.get(("i", start + i), 0) for i in range(n)]

    def read_holding_registers(self, a, start, n):
        self._chk()
        return [self.bank.regs.get(("h", start + i), 0) for i in range(n)]

    def read_coils(self, a, start, n):
        self._chk()
        return [self.bank.coils.get(start + i, 0) for i in range(n)]

    def read_discrete_inputs(self, a, start, n):
        self._chk()
        return [self.bank.discretes.get(start + i, 0) for i in range(n)]

    def write_register(self, a, reg, value):
        self._chk()
        self.bank.regs[("h", reg)] = value
        self.writes.append(("reg", reg, value))

    def write_coil(self, a, coil, value):
        self._chk()
        self.bank.coils[coil] = 1 if value else 0
        self.writes.append(("coil", coil, bool(value)))


def _bank():
    return Bank(regs={("i", 4000): 0x10000 - 55, ("h", 190): 220},
                coils={16: 0}, discretes={9: 1})


def _calls(pub):
    keep = ("pub_control", "pub_error", "device_online", "pub_meta",
            "pub_control_meta", "pub_control_units", "subscribe_writeback")
    out = []
    for c in pub.method_calls:
        if c[0] in keep:
            args = tuple(a for a in c[1] if not callable(a))
            out.append((c[0],) + args + tuple(sorted(c[2].items())))
    return out


class TestTemplateOverTcpEqualsRtu(_Isolated):
    """One scenario, two transports, one publish sequence."""

    CFG = {"offline_after_fails": 3, "backoff_base_s": 0.01, "poll_s": 0}

    def _scenario(self, poller, go_down, come_back):
        poller.setup()
        poller.poll_io()                                  # values
        tmpl = {c.name: c for c in poller._channels}
        with mock.patch.object(bridge_template, "DeviceLiveCache"):
            poller._writeback(tmpl["temp_setpoint"], "22.5")     # FC06
            poller._writeback(tmpl["run"], "1")                  # FC05
        poller.poll_io()                                  # echo held by grace
        go_down()
        for _ in range(2):
            poller._t_poll = 0.0
            poller.poll_io()                              # offline after 3 fails
        come_back()
        poller._t_poll = 0.0
        poller.poll_io()                                  # online again

    def test_same_publish_sequence(self):
        # RS-485 run.
        rtu_bank = _bank()
        rtu = _Rtu(rtu_bank)
        pub_rtu = mock.Mock()
        p_rtu = bridge_template.TemplatePoller(
            dict(self.CFG, id="dev", type="template", template="tcpt",
                 port="/dev/COM5", address=1), pub_rtu)
        p_rtu.get_port = lambda: rtu

        def rtu_down():
            rtu.down = True

        def rtu_up():
            rtu.down = False

        self._scenario(p_rtu, rtu_down, rtu_up)

        # Modbus TCP run: the poller's pooled client points at the fake server.
        tcp_bank = _bank()
        srv = FakeMbapServer(tcp_bank)
        self.addCleanup(srv.stop)
        client = bridge_tcp.ModbusTcpClient("127.0.0.1", srv.port, timeout=0.3,
                                            refuse_self=False)
        bridge_tcp._tcp_pool[("192.0.2.10", 502)] = client
        pub_tcp = mock.Mock()
        p_tcp = bridge_template.TemplatePoller(
            dict(self.CFG, **_tcp_template(id="dev")), pub_tcp)
        with mock.patch.object(bridge_serial, "get_port",
                               side_effect=AssertionError("serial port opened")):
            def tcp_down():
                srv.stop()

            def tcp_up():
                srv.start()
                # Past the client's reconnect window, as the port cycle would be.
                time.sleep(max(0.0, client._retry_at - time.monotonic()) + 0.05)

            with self.assertLogs("dev.dev", level="WARNING") as logs:
                self._scenario(p_tcp, tcp_down, tcp_up)
        client.close()

        seq_rtu, seq_tcp = _calls(pub_rtu), _calls(pub_tcp)
        self.assertEqual(seq_rtu, seq_tcp)
        # And the sequence is the one G-TCP promises, not merely equal.
        online = [c for c in seq_tcp if c[0] == "device_online"]
        self.assertEqual(online, [("device_online", "dev", False),
                                  ("device_online", "dev", True)])
        values = {c[2]: c[3] for c in seq_tcp if c[0] == "pub_control"}
        self.assertEqual(values["supply_temp"], "-5.5")
        self.assertEqual(values["alarm"], "1")
        # Writes landed on the device the same way on both transports.
        self.assertEqual(srv.writes(), [("reg", 190, 225), ("coil", 16, True)])
        self.assertEqual(rtu.writes, [("reg", 190, 225), ("coil", 16, True)])
        # The offline line names the endpoint and unit on TCP.
        self.assertTrue(any("192.0.2.10:502 unit 1" in ln for ln in logs.output),
                        logs.output)


if __name__ == "__main__":
    unittest.main()
