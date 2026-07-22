#!/usr/bin/env python3
"""CE-02m-3 power poll interval + MR display-name canonicalize."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))


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


class TestCanonicalMr02mName(unittest.TestCase):
    def test_rewrites_letter_first(self):
        cfg = {
            "port": "/dev/COM4",
            "address": 6,
            "name": "MR-02m AO6AI6 (COM4 addr=6)",
        }
        self.assertEqual(
            bridge._canonical_mr02m_device_name(cfg, "6AI6AO"),
            "MR-02m 6AI6AO (COM4 addr=6)",
        )

    def test_keeps_russian_count_first(self):
        cfg = {
            "port": "/dev/COM4",
            "address": 10,
            "name": "МР-02м 14ДИ (COM4 addr=10)",
        }
        self.assertEqual(
            bridge._canonical_mr02m_device_name(cfg, "14DI"),
            "МР-02м 14ДИ (COM4 addr=10)",
        )


class TestCE02PowerInterval(unittest.TestCase):
    def test_poll_io_honors_poll_power_s(self):
        pub = mock.Mock()
        cfg = {
            "id": "ce02m3-COM2-14",
            "type": "ce02m3",
            "port": "/dev/COM2",
            "address": 14,
            "poll_power_s": 5,
        }
        p = bridge.CE02M3Poller(cfg, pub)
        with mock.patch.object(p, "_poll_power") as pp:
            with mock.patch.object(
                bridge.time, "monotonic", side_effect=[100.0, 101.0, 106.0]
            ):
                p.poll_io()
                p.poll_io()  # within 5 s — skip
                p.poll_io()  # due again
        self.assertEqual(pp.call_count, 2)


class TestCE02FmbEvents(unittest.TestCase):
    def test_event_ranges(self):
        pub = mock.Mock()
        p = bridge.CE02M3Poller({
            "id": "ce02m3-COM2-14", "type": "ce02m3",
            "port": "/dev/COM2", "address": 14,
        }, pub)
        self.assertEqual(p.fmb_event_ranges(), [
            (bridge.FMB_EVT_INPUT, 500, 3),
            (bridge.FMB_EVT_INPUT, 510, 4),
        ])

    def test_dispatch_voltage_and_current(self):
        pub = mock.Mock()
        p = bridge.CE02M3Poller({
            "id": "ce02m3-COM2-14", "type": "ce02m3",
            "port": "/dev/COM2", "address": 14,
            "ct_ratio": 4000, "phases": ["A", "B", "C"],
        }, pub)
        p.fmb_dispatch(bridge.FMB_EVT_INPUT, 500, 2205)  # 220.5 V
        pub.pub_control.assert_any_call("ce02m3-COM2-14", "voltage_a", "220.5")
        p.fmb_dispatch(bridge.FMB_EVT_INPUT, 510, 1234)  # 1.234 A * 4.0 CT
        pub.pub_control.assert_any_call("ce02m3-COM2-14", "current_a", "4.936")


class TestFmbWbStyleInsurance(unittest.TestCase):
    def test_fmb_arms_500ms_insurance_intervals(self):
        pub = mock.Mock()
        p = bridge.MR02mPoller({
            "id": "mr02m-COM4-6", "type": "mr02m", "port": "/dev/COM4",
            "address": 6, "module_type": 6,
            "poll_do_di_s": 0, "poll_ai_ao_s": 0.2,
        }, pub)
        self.assertEqual(p._poll_do_di_s, 0.0)
        self.assertEqual(p._poll_ai_ao_s, 0.2)
        p.set_fmb_io_covered(True)
        self.assertGreaterEqual(p._poll_do_di_s, bridge.FMB_INSURANCE_POLL_S)
        self.assertGreaterEqual(p._poll_ai_ao_s, bridge.FMB_INSURANCE_POLL_S)
        p.set_fmb_io_covered(False)
        self.assertEqual(p._poll_do_di_s, 0.0)
        self.assertEqual(p._poll_ai_ao_s, 0.2)

    def test_event_period_scales_with_baud(self):
        self.assertEqual(
            bridge.FastModbusEventPortManager("/dev/COM4", 115200).event_period_s, 0.05)
        self.assertEqual(
            bridge.FastModbusEventPortManager("/dev/COM4", 38400).event_period_s, 0.10)
        self.assertEqual(
            bridge.FastModbusEventPortManager("/dev/COM4", 9600).event_period_s, 0.20)

    def test_port_cycle_owns_fmb_no_separate_run(self):
        self.assertTrue(hasattr(bridge.PortCycleScheduler, "run"))
        self.assertFalse(hasattr(bridge.FastModbusEventPortManager, "run"))
        self.assertIs(bridge.PortPollScheduler, bridge.PortCycleScheduler)


class TestFmbReconfigureBackoff(unittest.TestCase):
    """A1: a rebooted-then-silent device gets at most one reconfigure
    attempt per backoff window — never a 0.4 s timeout burn under the
    port lock on every ~50 ms event cycle."""

    def _mgr_with_pending_device(self):
        mgr = bridge.FastModbusEventPortManager("/dev/COM4", 115200)
        mgr.register_device(
            6, "mr02m-COM4-6", [(bridge.FMB_EVT_INPUT, 500, 3)], lambda *a: None)
        dev = mgr._devices[6]
        # State after a reboot event on a device that then went silent.
        dev["configured"] = False
        dev["pending"] = list(dev["ranges"])
        return mgr, dev

    def test_silent_device_one_attempt_per_backoff_window(self):
        mgr, _dev = self._mgr_with_pending_device()
        with mock.patch.object(bridge, "get_port",
                               return_value=mock.Mock()) as gp, \
                mock.patch.object(mgr, "_configure_device",
                                  return_value=False) as cd, \
                mock.patch.object(
                    bridge.time, "monotonic",
                    side_effect=[100.0, 100.05, 110.0,
                                 100.0 + bridge.FMB_RECONFIGURE_BACKOFF_S + 1.0]):
            mgr.reconfigure_pending()   # attempt fails -> backoff armed
            mgr.reconfigure_pending()   # ~50 ms later: skipped
            mgr.reconfigure_pending()   # 10 s later: still inside the window
            self.assertEqual(cd.call_count, 1)
            self.assertEqual(gp.call_count, 1)  # port not touched in backoff
            mgr.reconfigure_pending()   # past the window: retried
            self.assertEqual(cd.call_count, 2)

    def test_reboot_event_clears_backoff(self):
        mgr, dev = self._mgr_with_pending_device()
        dev["retry_at"] = 1e9   # deep inside a backoff window
        # A reboot event proves the device is talking — retry immediately.
        mgr._dispatch(6, bridge.FMB_EVT_REBOOT, 0, -1)
        self.assertEqual(dev["retry_at"], 0.0)
        self.assertFalse(dev["configured"])
        self.assertEqual(dev["pending"], dev["ranges"])


class TestDtvFmbInsurance(unittest.TestCase):
    """A2: while FMB events cover DTV regs 25-30 + coils, a low-rate classic
    insurance reread keeps them from going stale on a lost event."""

    def _poller(self):
        pub = mock.Mock()
        return bridge.DTVPoller({
            "id": "dtv-COM1-3", "type": "dtv",
            "port": "/dev/COM1", "address": 3,
        }, pub)

    def test_insurance_rereads_event_channels_low_rate(self):
        p = self._poller()
        p.set_fmb_io_covered(True)
        sensor_calls = []
        with mock.patch.object(p, "_poll_sensors",
                               side_effect=lambda **kw: sensor_calls.append(kw)), \
                mock.patch.object(p, "_poll_coils") as coils, \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"), \
                mock.patch.object(bridge.time, "monotonic",
                                  side_effect=[1000.0, 1001.0, 1031.0]):
            p.poll_io()   # hot 1..24 + first insurance 25..30 + coils
            p.poll_io()   # 1 s later: nothing due
            p.poll_io()   # 31 s later: insurance due again
        self.assertEqual(
            [kw for kw in sensor_calls if "from_reg" not in kw],
            [{"upto_reg": 24}, {"upto_reg": 24}])   # hot path never reads 25..30
        self.assertEqual(
            [kw for kw in sensor_calls if "from_reg" in kw],
            [{"from_reg": 25, "upto_reg": 30}] * 2)
        self.assertEqual(coils.call_count, 2)   # coils only via insurance

    def test_classic_full_read_without_fmb(self):
        p = self._poller()
        sensor_calls = []
        with mock.patch.object(p, "_poll_sensors",
                               side_effect=lambda **kw: sensor_calls.append(kw)), \
                mock.patch.object(p, "_poll_coils") as coils, \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"), \
                mock.patch.object(bridge.time, "monotonic",
                                  side_effect=[1000.0]):
            p.poll_io()
        self.assertEqual(sensor_calls, [{"upto_reg": 30}])
        coils.assert_called_once()


if __name__ == "__main__":
    unittest.main()
