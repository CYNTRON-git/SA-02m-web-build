#!/usr/bin/env python3
"""CarelPoller over Modbus TCP — the bench banks replayed behind a real socket.

Honesty label: the register VALUES are the real ones (read off the bench
controllers on 1.135 COM3, 2026-09-03 — the banks are imported unchanged from
test_carel_poller.py) and the FC17 replies are the captured frames under
opt/sa02m-carel/tests/fixtures. What this proves is OUR side over TCP: our
client, the poller's decode/publish set, the write plans and their order, the
audit trail, the clamp and the offline semantics. It does NOT prove a real
Carel TCP stack — whether a native Carel Ethernet interface answers FC17, which
unit id it expects, or whether it takes the uAria two-word FC16 is unverified
on hardware (docs/contracts/bridge-modbus-tcp.md, carel-ahu.md §9).
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

import test_carel_poller as rtu  # noqa: E402  (stubs + sys.path, banks)
import bridge_carel  # noqa: E402
import bridge_serial  # noqa: E402
import bridge_tcp  # noqa: E402
from mbap_fake import Bank, FakeMbapServer  # noqa: E402
from sa02m_carel import carel_ahu as ca  # noqa: E402
from sa02m_carel import controls as cc  # noqa: E402

FIXTURES = TESTS_DIR.parents[1] / "sa02m-carel" / "tests" / "fixtures"
HOST = "192.168.1.50"


def _fc17(name: str) -> bytes:
    """Captured FC17 payload (the c.pCOmini capture is a whole RTU frame)."""
    raw = bytes.fromhex((FIXTURES / name).read_text(encoding="ascii").strip())
    if len(raw) > 5 and raw[1] == 0x11 and len(raw) == 3 + raw[2] + 2:
        return raw[3: 3 + raw[2]]
    return raw


FC17_CRST = _fc17("fc17_probe2.hex")      # CRSTDrAHAQ 2.03.00.46
FC17_UARIA = _fc17("fc17_crstdm.hex")     # CRSTDm_AHU


class _TcpCase(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(bridge_tcp._tcp_pool, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def poller(self, family, bank, fc17=b"", **cfg):
        regs, coils, discretes = bank
        self.bank = Bank(regs, coils, discretes, fc17=fc17)
        self.srv = FakeMbapServer(self.bank)
        self.addCleanup(self.srv.stop)
        self.client = bridge_tcp.ModbusTcpClient("127.0.0.1", self.srv.port,
                                                 timeout=0.5, refuse_self=False)
        self.addCleanup(self.client.close)
        bridge_tcp._tcp_pool[(HOST, 502)] = self.client
        base = {"id": "carel-tcp-192_168_1_50-1", "type": "carel",
                "family": family, "transport": "tcp", "host": HOST,
                "tcp_port": 502, "address": 1, "app_version": "2.03.00.46"}
        base.update(cfg)
        for k in [k for k, v in base.items() if v is None]:
            del base[k]
        self.pub = mock.Mock()
        return bridge_carel.CarelPoller(base, self.pub)


class TestSetup(_TcpCase):
    def test_setup_over_tcp_names_the_endpoint(self):
        # RED on the pre-1.0.6.56 poller: the default name was built from
        # port_path.replace(...), and a TCP poller has no port path.
        p = self.poller("crst", rtu.crst_bank())
        with mock.patch.object(bridge_serial, "get_port",
                               side_effect=AssertionError("serial port opened")):
            p.setup()
        meta = {c.args[1]: c.args[2] for c in self.pub.pub_meta.call_args_list}
        self.assertEqual(meta["name"], "Carel c.pCOmini (192.168.1.50:502 addr=1)")
        self.assertEqual(meta["driver"], "carel")
        subscribed = [c.args[1] for c in self.pub.subscribe_writeback.call_args_list]
        self.assertEqual(sorted(subscribed), sorted(cc.writable_names("crst")))

    def test_rtu_name_is_unchanged(self):
        p, pub, _ser = rtu._poller("crst", rtu.crst_bank())
        p.setup()
        meta = {c.args[1]: c.args[2] for c in pub.pub_meta.call_args_list}
        self.assertEqual(meta["name"], "Carel c.pCOmini (COM3 addr=1)")


class TestIdentity(_TcpCase):
    def test_fc17_replay_fills_the_version(self):
        p = self.poller("crst", rtu.crst_bank(), fc17=FC17_CRST, app_version=None)
        with self.assertLogs(p.log.name, level="INFO") as logs:
            p.setup()
        self.assertEqual(p._version, (2, 3, 0, 46))
        self.assertTrue(any("carel identity: CRSTDrAHAQ (crst) v2.03.00.46" in ln
                            for ln in logs.output), logs.output)
        self.assertFalse([ln for ln in logs.output if ln.startswith("WARNING")])

    def test_uaria_fc17_replay(self):
        p = self.poller("uaria", rtu.uaria_bank(), fc17=FC17_UARIA, address=2,
                        app_version=None)
        with self.assertLogs(p.log.name, level="INFO") as logs:
            p.setup()
        self.assertEqual(p.family, "uaria")
        self.assertTrue(any("CRSTDm_AHU (uaria)" in ln for ln in logs.output))

    def test_no_fc17_keeps_the_family_and_warns_once_about_the_table(self):
        p = self.poller("crst", rtu.crst_bank(), fc17=b"", app_version=None)
        with self.assertLogs(p.log.name, level="WARNING") as logs:
            p.setup()
        self.assertEqual(p.family, "crst")
        self.assertIsNone(p._version)
        warns = [ln for ln in logs.output if "app_version" in ln]
        self.assertEqual(len(warns), 1, logs.output)
        self.assertIn("v2", warns[0])
        # The exception reply is an answer, not a transport failure.
        self.assertEqual(self.srv.fcs()[0], 17)

    def test_a_given_app_version_needs_no_warning(self):
        p = self.poller("crst", rtu.crst_bank(), fc17=b"")
        with self.assertNoLogs(p.log.name, level="WARNING"):
            p.setup()

    def test_rtu_carel_without_version_is_not_warned(self):
        # The WARN is about FC17 over TCP being unverified; RS-485 is unchanged.
        p, _pub, _ser = rtu._poller("crst", rtu.crst_bank(), app_version="")
        with self.assertNoLogs(p.log.name, level="WARNING"):
            p.setup()


class TestPublishSetEqualsRtu(_TcpCase):
    def _rtu_run(self, family, bank, **cfg):
        p, pub, _ser = rtu._poller(family, bank, **cfg)
        p.poll_io()
        return rtu._published(pub), rtu._errors(pub)

    def test_crst(self):
        want = self._rtu_run("crst", rtu.crst_bank())
        p = self.poller("crst", rtu.crst_bank())
        p.poll_io()
        got = (rtu._published(self.pub), rtu._errors(self.pub))
        self.assertEqual(got, want)
        self.assertEqual(got[0]["supply_temp"], "26.5")
        self.assertEqual(got[1].get("room_temp"), "r")      # unfitted probe

    def test_uaria(self):
        want = self._rtu_run("uaria", rtu.uaria_bank(), address=2, app_version="")
        p = self.poller("uaria", rtu.uaria_bank(), address=2, app_version=None)
        p.poll_io()
        got = (rtu._published(self.pub), rtu._errors(self.pub))
        self.assertEqual(got, want)
        self.assertEqual(got[0]["supply_temp"], "27.22")

    def test_alarm_pack_fallback_when_the_17_register_read_is_rejected(self):
        regs, coils, discretes = rtu.crst_bank()
        regs[("i", 301)] = 1 << 1                       # E01
        p = self.poller("crst", (regs, coils, discretes))
        self.bank.reject[(4, 301, 17)] = 2              # exception 02
        p.poll_io()
        v = rtu._published(self.pub)
        self.assertEqual(v["alarm"], "1")
        self.assertIn("E01", v["alarm_text"])
        pdus = [r[3] for r in self.srv.requests if r[2] == 4]
        self.assertIn(bytes([4, 1, 45, 0, 17]), pdus)   # the rejected one…
        self.assertIn(bytes([4, 1, 45, 0, 10]), pdus)   # …then the fallback


class TestWritePlans(_TcpCase):
    def test_crst_start_is_coil_130_settle_coil_65(self):
        p = self.poller("crst", rtu.crst_bank())
        at_sleep = []
        with mock.patch.object(bridge_carel.time, "sleep",
                               side_effect=lambda s: at_sleep.append(
                                   (s, list(self.srv.writes())))):
            p._writeback("unit_on", "1")
        self.assertEqual(self.srv.writes(), [("coil", 130, True), ("coil", 65, True)])
        # The settle ran after coil 130 reached the PLC and before coil 65.
        self.assertIn((ca.START_MA18_SETTLE_S, [("coil", 130, True)]), at_sleep)

    def test_uaria_setpoint_is_one_fc16(self):
        p = self.poller("uaria", rtu.uaria_bank(), address=2)
        p._writeback("setpoint", "23.5")
        self.assertEqual(self.srv.writes(),
                         [("regs", ca.HR_UARIA_SP, list(ca.float32_to_be_words(23.5)))])
        self.assertEqual(self.srv.fcs().count(6), 0)
        self.assertEqual(ca.be_float32(self.bank.regs[("h", 30)],
                                       self.bank.regs[("h", 31)]), 23.5)

    def test_fan_out_of_range_is_clamped_and_audited(self):
        p = self.poller("crst", rtu.crst_bank())
        with self.assertLogs(p.log.name, level="INFO") as logs:
            p._writeback("fan_supply", "150")
        self.assertEqual(self.srv.writes(),
                         [("reg", ca.HR_FAN_SUPPLY,
                           ca.phys_to_raw_x10(ca.FAN_PCT_MAX, ca.FAN_PCT_MIN,
                                              ca.FAN_PCT_MAX))])
        self.assertTrue(any("applied" in ln and "'150'" in ln for ln in logs.output),
                        logs.output)

    def test_every_accepted_write_leaves_one_audit_line(self):
        for family, address, bank in (("crst", 1, rtu.crst_bank()),
                                      ("uaria", 2, rtu.uaria_bank())):
            for name in cc.writable_names(family):
                with self.subTest(family=family, name=name):
                    p = self.poller(family, bank, address=address)
                    with self.assertLogs(p.log.name, level="INFO") as logs:
                        with mock.patch.object(bridge_carel.time, "sleep"):
                            p._writeback(name, "1")
                    lines = [ln for ln in logs.output if "requested" in ln]
                    self.assertEqual(len(lines), 1, logs.output)
                    self.assertTrue(self.srv.writes(), name)

    def test_the_local_terminal_coil_is_never_written(self):
        p = self.poller("uaria", rtu.uaria_bank(), address=2)
        with mock.patch.object(bridge_carel.time, "sleep"):
            for name, payload in (("unit_on", "1"), ("unit_on", "0"),
                                  ("net_enable", "1"), ("net_enable", "0"),
                                  ("setpoint", "23.5"), ("setpoint_summer", "21"),
                                  ("fan_step", "5")):
                p._writeback(name, payload)
        writes = self.srv.writes()
        self.assertTrue(writes)
        self.assertEqual([w for w in writes if w[0] == "coil" and w[1] == 30], [])


class TestOfflineEqualsRtu(_TcpCase):
    def test_same_availability_sequence(self):
        cfg = {"offline_after_fails": 3, "backoff_base_s": 0.01, "poll_s": 0}
        # RS-485: the line goes silent, then answers again.
        p_rtu, pub_rtu, ser = rtu._poller("crst", rtu.crst_bank(), **cfg)
        good = ser.read_input_registers

        def dead(*_a, **_k):
            raise IOError("Short response: 0/13 bytes")

        ser.read_input_registers = dead
        for _ in range(3):
            p_rtu.poll_io()
        ser.read_input_registers = good
        p_rtu.poll_io()

        # Modbus TCP: the device goes away (session reset, connect refused).
        p = self.poller("crst", rtu.crst_bank(), **cfg)
        p.poll_io()
        self.pub.reset_mock()
        pub_rtu_seq = [c for c in pub_rtu.method_calls if c[0] == "device_online"]
        self.srv.stop()
        for _ in range(3):
            p.poll_io()
        self.srv.start()
        time.sleep(max(0.0, self.client._retry_at - time.monotonic()) + 0.05)
        p.poll_io()
        tcp_seq = [c for c in self.pub.method_calls if c[0] == "device_online"]
        self.assertEqual([c.args[1] for c in tcp_seq],
                         [c.args[1] for c in pub_rtu_seq])
        self.assertEqual([c.args for c in tcp_seq],
                         [("carel-tcp-192_168_1_50-1", False),
                          ("carel-tcp-192_168_1_50-1", True)])


if __name__ == "__main__":
    unittest.main()
