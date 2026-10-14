#!/usr/bin/env python3
"""TemplatePoller — a silent device stops costing the line, and block reads.

Pins docs/contracts/template-device.md §3 (grouping fields) and §5 (a silent
device): G1 a template device that never answered costs at most
`offline_after_fails` transactions on its first pass and one per back-off
window after it, while a device that answered in this pass or the previous one
is read in full — it stays online and writable despite broken channels;
G2 a template without grouping fields is polled exactly as before (one request
per channel, template order); G3 `max_read_registers` > 1 reads the declared
registers in blocks, and a block the device refuses while its channels read
singly is split back into single reads.

Honesty label: the transports here are a FakeSerial and an in-process MBAP
server — the tests prove the poll logic and the request sequence, NOT how a
real device answers a long request (docs/contracts/template-device.md §7).
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
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
import bridge_tcp  # noqa: E402
import bridge_template  # noqa: E402
from mbap_fake import Bank, FakeMbapServer  # noqa: E402

DEVICE_ID = "blk-COM5-30"
LOGGER = f"dev.{DEVICE_ID}"


class FakeSerial:
    """Register/bit banks + a journal of every read as (fc, start, count).

    dead       — every read raises (a device off the line);
    fail_addrs — a read STARTING at one of these addresses raises;
    max_count  — a read of more than this many registers/bits raises (a device
                 that refuses a long request).
    """

    def __init__(self, regs=None, coils=None, discretes=None,
                 dead=False, fail_addrs=(), max_count=None):
        self.regs = regs or {}
        self.coils = coils or {}
        self.discretes = discretes or {}
        self.dead = dead
        self.fail_addrs = set(fail_addrs)
        self.max_count = max_count
        self.reads = []

    def _read(self, fc, bank, start, count):
        self.reads.append((fc, start, count))
        if self.dead:
            raise IOError("Short response: 0/7 bytes")
        if start in self.fail_addrs:
            raise IOError(f"timeout reg={start}")
        if self.max_count is not None and count > self.max_count:
            raise IOError(f"Modbus exception 2 on FC{fc:02X}")
        return [bank.get(start + i, 0) for i in range(count)]

    def read_coils(self, addr, start, count):
        return self._read(1, self.coils, start, count)

    def read_discrete_inputs(self, addr, start, count):
        return self._read(2, self.discretes, start, count)

    def read_holding_registers(self, addr, start, count):
        return self._read(3, self.regs, start, count)

    def read_input_registers(self, addr, start, count):
        return self._read(4, self.regs, start, count)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _poller(self, channels, device_extra=None, **cfg):
        device = {"name": "blk", "channels": channels}
        device.update(device_extra or {})
        (self.dir / "config-blk.json").write_text(
            json.dumps({"device": device}), encoding="utf-8")
        base = {"id": DEVICE_ID, "type": "template", "template": "blk",
                "port": "/dev/COM5", "address": 30}
        if cfg.get("transport") == "tcp":
            del base["port"]            # serial keys are refused on a TCP entry
        base.update(cfg)
        pub = mock.Mock()
        with mock.patch.dict("os.environ",
                             {"SA02M_WB_TEMPLATES_DIR": str(self.dir)}):
            return bridge_template.TemplatePoller(base, pub), pub

    @staticmethod
    def _pass(p, fake):
        """One due poll pass (the poll_s cadence is not under test here)."""
        p._t_poll = -1e9
        with mock.patch.object(p, "get_port", return_value=fake), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            p.poll_io()

    @staticmethod
    def _values(pub):
        return {c.args[1]: c.args[2] for c in pub.pub_control.call_args_list}


def _u16(name, address, reg_type="holding"):
    return {"name": name, "reg_type": reg_type, "address": address,
            "format": "u16"}


SIX_HOLDING = [_u16(f"c{i}", i) for i in range(6)]


class TestSilentDevice(_Base):
    """G1 + D2: a dead device stops the pass; the next pass resumes after it."""

    def test_dead_device_first_pass_stops_at_threshold(self):
        p, pub = self._poller(SIX_HOLDING, offline_after_fails=3)
        fake = FakeSerial(dead=True)
        self._pass(p, fake)
        self.assertEqual(len(fake.reads), 3, fake.reads)
        self.assertFalse(p._online)
        # The three channels the pass no longer reads are flagged stale all the
        # same: every channel ends the pass at "r", as before the stop.
        for i in range(6):
            pub.pub_error.assert_any_call(DEVICE_ID, f"c{i}", "r")

    def test_dead_device_next_window_costs_one_read(self):
        p, _pub = self._poller(SIX_HOLDING, offline_after_fails=3)
        fake = FakeSerial(dead=True)
        self._pass(p, fake)
        fake.reads.clear()
        p._backoff_until = 0.0          # the back-off window has expired
        self._pass(p, fake)
        # One transaction, on the channel after the one the last pass stopped at.
        self.assertEqual(fake.reads, [(3, 3, 1)])

    def test_stalled_head_does_not_starve_tail(self):
        # Channels 0-2 never answer, 3-5 do: the device is alive, but three
        # consecutive failures cross offline_after_fails at channel 2. Without
        # resuming after the stall every pass would stop there.
        p, pub = self._poller(SIX_HOLDING, offline_after_fails=3)
        fake = FakeSerial(regs={3: 30, 4: 40, 5: 50}, fail_addrs={0, 1, 2})
        self._pass(p, fake)
        pub.pub_control.reset_mock()
        p._backoff_until = 0.0
        self._pass(p, fake)
        values = self._values(pub)
        self.assertEqual((values.get("c3"), values.get("c4"), values.get("c5")),
                         ("30", "40", "50"))

    def test_dead_device_costs_one_read_per_window(self):
        p, _pub = self._poller(SIX_HOLDING, offline_after_fails=3)
        fake = FakeSerial(dead=True)
        self._pass(p, fake)
        self.assertEqual(len(fake.reads), 3, fake.reads)
        for _ in range(8):
            fake.reads.clear()
            p._backoff_until = 0.0
            self._pass(p, fake)
            self.assertEqual(len(fake.reads), 1, fake.reads)
        self.assertFalse(p._online)


class TestLiveDeviceStaysOnline(_Base):
    """Review B1: the early stop never leaves a device that answers offline.

    A pass stops early only when nothing answered in it nor in the previous
    pass. A live device with broken channels therefore reads every channel
    each pass and ends it online, so its writes are not refused
    ("writeback skipped: device offline") and its healthy channels never flip
    to "r" — the same as before the stop existed (plan §5.3: meta/error,
    back-off and writes unchanged)."""

    def _window(self, p, fake):
        p._backoff_until = 0.0
        fake.reads.clear()
        self._pass(p, fake)
        return list(fake.reads)

    def test_live_device_with_broken_head_ends_pass_online(self):
        p, pub = self._poller(SIX_HOLDING, offline_after_fails=3)
        fake = FakeSerial(regs={3: 30, 4: 40, 5: 50}, fail_addrs={0, 1, 2})
        # Bridge start (contract §5): nothing has answered yet, so the first
        # pass stops at c2 and flags the rest once; the second resumes at c3
        # and ends on the failing run; from the third on, a full pass.
        self.assertEqual(self._window(p, fake), [(3, 0, 1), (3, 1, 1), (3, 2, 1)])
        self.assertFalse(p._online)
        self.assertEqual(self._window(p, fake), [(3, i, 1) for i in (3, 4, 5, 0, 1, 2)])
        self.assertFalse(p._online)
        self._window(p, fake)
        self.assertTrue(p._online, "device left offline at rest")
        stale = [c.args[1] for c in pub.pub_error.call_args_list
                 if c.args[2] == "r" and c.args[1] in ("c3", "c4", "c5")]
        self.assertEqual(stale, ["c3", "c4", "c5"])     # once, at the start
        self.assertFalse(p.in_backoff())
        # At rest every pass reads every channel, in template order.
        self.assertEqual(self._window(p, fake), [(3, i, 1) for i in range(6)])
        self.assertTrue(p._online)

    def test_grouped_device_answering_only_in_blocks_stays_online(self):
        # A good block read is an answer too: three failing singles between
        # two good blocks do not stop the pass, from the very first one.
        channels = [_u16("a", 0, "input"), _u16("b", 1, "input"),
                    _u16("x", 100), _u16("y", 200), _u16("z", 300),
                    _u16("c", 10, "input"), _u16("d", 11, "input")]
        p, pub = self._poller(channels, {"max_read_registers": 10})
        fake = FakeSerial(regs={0: 1, 1: 2, 10: 3, 11: 4},
                          fail_addrs={100, 200, 300})
        wire = [(4, 0, 2), (3, 100, 1), (3, 200, 1), (3, 300, 1), (4, 10, 2)]
        for n in range(1, 4):
            with self.subTest(pass_no=n):
                self.assertEqual(self._window(p, fake), wire)
                self.assertTrue(p._online)
                self.assertFalse(p.in_backoff())
        self.assertFalse([c for c in pub.pub_error.call_args_list
                          if c.args[2] == "r" and c.args[1] in "abcd"])

    def test_refused_block_after_a_failing_run_is_swept_in_the_same_pass(self):
        # The device answered (w), then a failing run put it in back-off: the
        # refused block that follows — whose own first channel is broken — is
        # still swept singly in this pass, not deferred to the next one.
        channels = [_u16("w", 500), _u16("x", 100), _u16("y", 200),
                    _u16("z", 300), _u16("e", 0, "input"),
                    _u16("a", 1, "input"), _u16("b", 2, "input")]
        p, pub = self._poller(channels, {"max_read_registers": 10})
        fake = FakeSerial(regs={500: 5, 1: 11, 2: 12},
                          fail_addrs={100, 200, 300, 0})
        wire = [(3, 500, 1), (3, 100, 1), (3, 200, 1), (3, 300, 1),
                (4, 0, 3), (4, 0, 1), (4, 1, 1), (4, 2, 1)]
        self.assertEqual(self._window(p, fake), wire)
        self.assertEqual({k: v for k, v in self._values(pub).items()
                          if k in "ab"}, {"a": "11", "b": "12"})
        self.assertTrue(p._online)
        self.assertFalse([c for c in pub.pub_error.call_args_list
                          if c.args[2] == "r" and c.args[1] in "wab"])

    def test_mp02_with_four_failing_setpoints_matches_the_ungrouped_wire(self):
        # The shipped MP-02 template (24 channels) on firmware that does not
        # serve HR 190..193: four consecutive failures mid-pass, healthy
        # channels on both sides. Every pass must read all 24 channels in
        # template order — the pre-stop wire — and end with the device online.
        # The scheduler skips a device in back-off, so no window is forced
        # open here: a pass that ended in back-off would stop the loop.
        failing = {190, 191, 192, 193}
        with mock.patch.dict("os.environ",
                             {"SA02M_WB_TEMPLATES_DIR":
                              str(BRIDGE_DIR / "templates")}):
            p = bridge_template.TemplatePoller(
                {"id": DEVICE_ID, "type": "template", "template": "mp02-ahu",
                 "port": "/dev/COM5", "address": 1}, mock.Mock())
        pub = p.pub
        wire = [(1 if ch.reg_type == "coil" else
                 3 if ch.reg_type == "holding" else 4, ch.address, ch.words)
                for ch in p._channels]
        self.assertEqual(len(wire), 24)
        healthy = [ch.name for ch in p._channels if ch.address not in failing]
        self.assertEqual(len(healthy), 20)
        fake = FakeSerial(fail_addrs=failing)
        for n in range(1, 6):
            with self.subTest(pass_no=n):
                self.assertFalse(p.in_backoff(), "scheduler would skip the pass")
                fake.reads.clear()
                self._pass(p, fake)
                self.assertEqual(fake.reads, wire)
                self.assertTrue(p._online)
        flipped = sorted({c.args[1] for c in pub.pub_error.call_args_list
                          if c.args[2] == "r" and c.args[1] in healthy})
        self.assertEqual(flipped, [], "healthy channels flagged stale")

    def test_device_that_goes_silent_in_operation_costs_full_passes_first(self):
        # The cost of the rule, pinned: a pass is read in full while the
        # device answered in it or in the previous one (as before the stop
        # existed). Silent from a pass boundary → one full silent pass;
        # silent midway through a pass → that pass is read in full too. Then
        # one read per window.
        for label, last_live_pass in (
                ("between passes", FakeSerial()),
                ("midway", FakeSerial(fail_addrs={2, 3, 4, 5}))):
            with self.subTest(label):
                p, _pub = self._poller(SIX_HOLDING, offline_after_fails=3)
                self._window(p, FakeSerial())
                self.assertEqual(len(self._window(p, last_live_pass)), 6)
                fake = FakeSerial(dead=True)
                self.assertEqual(len(self._window(p, fake)), 6)
                self.assertFalse(p._online)
                for _ in range(4):
                    self.assertEqual(len(self._window(p, fake)), 1)


MIXED = [
    {"name": "sp", "reg_type": "holding", "address": 10, "format": "u16"},
    {"name": "energy", "reg_type": "input", "address": 0, "format": "u32"},
    {"name": "relay", "reg_type": "coil", "address": 5},
    {"name": "volt", "reg_type": "input", "address": 2, "format": "u16"},
    {"name": "alarm", "reg_type": "discrete", "address": 1},
    {"name": "freq", "reg_type": "holding", "address": 11, "format": "float"},
    {"name": "cur", "reg_type": "input", "address": 3, "format": "u16"},
    # Same register as "sp", another reading of it: grouping would merge the
    # two into one request, so today's two requests pin the ungrouped path.
    {"name": "sp_signed", "reg_type": "holding", "address": 10, "format": "s16"},
]
MIXED_WIRE = [(3, 10, 1), (4, 0, 2), (1, 5, 1), (4, 2, 1), (2, 1, 1),
              (3, 11, 2), (4, 3, 1), (3, 10, 1)]


class TestWireIdentity(_Base):
    """G2: no grouping field (or max_read_registers 1) → today's wire exactly."""

    def test_no_grouping_fields_is_wire_identical(self):
        for extra in (None, {"max_read_registers": 1},
                      {"max_read_registers": 1, "max_reg_hole": 50,
                       "max_bit_hole": 50}):
            with self.subTest(extra=extra):
                p, _pub = self._poller(MIXED, extra)
                fake = FakeSerial()
                self._pass(p, fake)
                self._pass(p, fake)
                self.assertEqual(fake.reads, MIXED_WIRE + MIXED_WIRE)


FOUR_INPUT = [
    {"name": "a", "reg_type": "input", "address": 0, "format": "u16"},
    {"name": "b", "reg_type": "input", "address": 1, "format": "u16"},
    {"name": "c", "reg_type": "input", "address": 2, "format": "u32"},
    {"name": "d", "reg_type": "input", "address": 10, "format": "u16"},
]


class TestGrouping(_Base):
    """G3: blocks per WB rules — contiguous, holes, the per-request cap."""

    def _reads(self, channels, extra, fake=None):
        p, pub = self._poller(channels, extra)
        fake = fake or FakeSerial()
        self._pass(p, fake)
        return fake.reads, pub

    def test_contiguous_channels_grouped(self):
        reads, _pub = self._reads(FOUR_INPUT, {"max_read_registers": 10})
        self.assertEqual(reads, [(4, 0, 4), (4, 10, 1)])

    def test_reg_hole_merges_gap(self):
        # 10 <= 3 + 1 + 6: the 6-register hole is read and discarded. The cap
        # counts the hole (11 registers), so it is set above 10 here.
        fake = FakeSerial(regs={0: 1, 1: 2, 2: 0, 3: 7, 10: 9})
        reads, pub = self._reads(
            FOUR_INPUT, {"max_read_registers": 16, "max_reg_hole": 6}, fake)
        self.assertEqual(reads, [(4, 0, 11)])
        self.assertEqual(self._values(pub),
                         {"a": "1", "b": "2", "c": "7", "d": "9"})
        # One register short of bridging the gap → two requests.
        reads, _pub = self._reads(
            FOUR_INPUT, {"max_read_registers": 16, "max_reg_hole": 5})
        self.assertEqual(reads, [(4, 0, 4), (4, 10, 1)])

    def test_cap_splits_group(self):
        # A channel is never split: the u32 at 2 would make the block 4 > 3.
        reads, _pub = self._reads(FOUR_INPUT, {"max_read_registers": 3})
        self.assertEqual(reads, [(4, 0, 2), (4, 2, 2), (4, 10, 1)])

    def test_zero_and_oversize_clamp_to_protocol_max(self):
        build = bridge_template._build_read_units

        def ch(reg_type, address):
            return bridge_template.TemplateChannel(
                name=f"{reg_type}{address}", reg_type=reg_type, address=address,
                fmt="u16" if reg_type in ("input", "holding") else "bit",
                word_order="big_endian", scale=1, offset=0, wb_type="value",
                units="", readonly=True, words=1)

        def spans(units):
            return [(u.reg_type, u.start, u.count) for u in units]

        regs = [ch("input", 0), ch("input", 124), ch("input", 125)]
        for max_read in (0, 500):
            with self.subTest(kind="regs", max_read=max_read):
                self.assertEqual(spans(build(regs, max_read, 1000, 1000)),
                                 [("input", 0, 125), ("input", 125, 1)])
        bits = [ch("coil", 0), ch("coil", 1999), ch("coil", 2000)]
        for max_read in (0, 5000):
            with self.subTest(kind="bits", max_read=max_read):
                self.assertEqual(spans(build(bits, max_read, 1000, 5000)),
                                 [("coil", 0, 2000), ("coil", 2000, 1)])
        # Within the protocol maximum the integrator's value is the cap.
        bits = [ch("coil", 0), ch("coil", 499), ch("coil", 500)]
        self.assertEqual(spans(build(bits, 500, 0, 5000)),
                         [("coil", 0, 500), ("coil", 500, 1)])

    def test_bit_grouping_with_bit_hole(self):
        coils = [{"name": f"k{a}", "reg_type": "coil", "address": a}
                 for a in (2, 3, 16)]
        fake = FakeSerial(coils={2: 1, 3: 0, 16: 1})
        reads, pub = self._reads(coils, {"max_read_registers": 20}, fake)
        self.assertEqual(reads, [(1, 2, 2), (1, 16, 1)])
        fake = FakeSerial(coils={2: 1, 3: 0, 16: 1})
        reads, pub = self._reads(
            coils, {"max_read_registers": 20, "max_bit_hole": 12}, fake)
        self.assertEqual(reads, [(1, 2, 15)])
        self.assertEqual(self._values(pub), {"k2": "1", "k3": "0", "k16": "1"})

    def test_group_slice_decodes_like_single(self):
        channels = [
            {"name": "volt", "reg_type": "input", "address": 0,
             "format": "u16", "scale": 0.1, "units": "V"},
            {"name": "freq", "reg_type": "input", "address": 1,
             "format": "float", "word_order": "big_endian", "units": "Hz"},
            {"name": "power", "reg_type": "input", "address": 3,
             "format": "s32", "word_order": "little_endian"},
        ]
        regs = {0: 2301, 1: 0x4248, 2: 0x0000, 3: 0xFFCE, 4: 0xFFFF}
        single_reads, single_pub = self._reads(channels, None,
                                               FakeSerial(regs=regs))
        group_reads, group_pub = self._reads(
            channels, {"max_read_registers": 10}, FakeSerial(regs=regs))
        self.assertEqual(len(single_reads), 3)
        self.assertEqual(group_reads, [(4, 0, 5)])
        self.assertEqual(self._values(group_pub), self._values(single_pub))
        self.assertEqual(self._values(group_pub),
                         {"volt": "230.1", "freq": "50.0", "power": "-50"})


class TestRejectedBlock(_Base):
    """D1: a block refused twice while its channels read singly is split."""

    def test_rejected_group_falls_back_then_splits(self):
        p, pub = self._poller([_u16(n, a, "input")
                               for n, a in (("a", 0), ("b", 1), ("c", 2), ("d", 3))],
                              {"max_read_registers": 10})
        fake = FakeSerial(regs={0: 10, 1: 11, 2: 12, 3: 13}, max_count=1)
        singles = [(4, 0, 1), (4, 1, 1), (4, 2, 1), (4, 3, 1)]
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            for n in (1, 2):
                fake.reads.clear()
                pub.pub_control.reset_mock()
                self._pass(p, fake)
                with self.subTest(pass_no=n):
                    # The block is tried, refused, and the channels still read.
                    self.assertEqual(fake.reads, [(4, 0, 4)] + singles)
                    self.assertEqual(self._values(pub),
                                     {"a": "10", "b": "11", "c": "12", "d": "13"})
                    self.assertTrue(p._online)
            fake.reads.clear()
            self._pass(p, fake)
            self.assertEqual(fake.reads, singles)   # the block is not asked again
            fake.reads.clear()
            self._pass(p, fake)
            self.assertEqual(fake.reads, singles)
        split_lines = [ln for ln in cm.output if "singly" in ln]
        self.assertEqual(len(split_lines), 1, cm.output)
        self.assertFalse([ln for ln in cm.output if "offline" in ln], cm.output)
        self.assertNotIn(mock.call(DEVICE_ID, "a", "r"),
                         pub.pub_error.call_args_list)

    def test_intermittent_block_refusal_does_not_split(self):
        # Review A2 (contract §3 «Счёт обнуляется успешным чтением пачки»): a
        # refusal, then a good block read, then a refusal is not two refusals
        # in a row — the block stays a block.
        p, _pub = self._poller([_u16("a", 0, "input"), _u16("b", 1, "input")],
                               {"max_read_registers": 10})
        fake = FakeSerial(regs={0: 10, 1: 11}, max_count=1)
        self._pass(p, fake)                       # refused, singles read: 1
        fake.max_count = None
        self._pass(p, fake)                       # block read OK: count reset
        fake.max_count = 1
        self._pass(p, fake)                       # refused again: 1, not 2
        fake.max_count = None
        fake.reads.clear()
        self._pass(p, fake)
        self.assertEqual(fake.reads, [(4, 0, 2)])

    def test_dead_device_grouped_stops_at_threshold(self):
        p, pub = self._poller([_u16(f"c{i}", i, "input") for i in range(6)],
                              {"max_read_registers": 10},
                              offline_after_fails=3)
        fake = FakeSerial(dead=True)
        self._pass(p, fake)
        self.assertEqual(fake.reads, [(4, 0, 6), (4, 0, 1), (4, 1, 1)])
        self.assertFalse(p._online)
        # Every channel of the block says it is stale, read or not.
        for i in range(6):
            pub.pub_error.assert_any_call(DEVICE_ID, f"c{i}", "r")


class TestStallOnBlock(_Base):
    """Review round 1 F1: a pass that stalls on a BLOCK read must not strand
    the block's channels. The next pass reads them singly (from where the last
    one stopped), so values arrive, D1 can split the block, and a dead device
    still costs one read per back-off window (G1)."""

    ABC = [_u16(n, a, "input") for n, a in (("a", 0), ("b", 1), ("c", 2))]

    def _window(self, p, fake):
        """One pass after the back-off window expired; returns its reads."""
        p._backoff_until = 0.0
        fake.reads.clear()
        self._pass(p, fake)
        return list(fake.reads)

    def test_threshold_one_refused_block_recovers(self):
        p, pub = self._poller(self.ABC, {"max_read_registers": 10},
                              offline_after_fails=1)
        fake = FakeSerial(regs={0: 10, 1: 11, 2: 12}, max_count=1)
        passes = [self._window(p, fake) for _ in range(6)]
        self.assertEqual(self._values(pub), {"a": "10", "b": "11", "c": "12"})
        self.assertTrue(p._online)
        # D1 still splits it: the last pass asks no block.
        self.assertEqual(passes[-1], [(4, 0, 1), (4, 1, 1), (4, 2, 1)], passes)

    def test_stall_landing_on_refused_block_is_read(self):
        # Two holding channels that never answer, then an input block the
        # device refuses: the third failure lands on the block read.
        channels = [_u16("x", 100), _u16("y", 101),
                    _u16("a", 0, "input"), _u16("b", 1, "input"),
                    _u16("z", 200)]
        p, pub = self._poller(channels, {"max_read_registers": 10})
        fake = FakeSerial(regs={0: 10, 1: 11, 200: 7}, fail_addrs={100, 101},
                          max_count=1)
        for _ in range(4):
            self._window(p, fake)
        values = self._values(pub)
        self.assertEqual((values.get("a"), values.get("b"), values.get("z")),
                         ("10", "11", "7"), values)

    def test_next_pass_starts_with_the_stalled_block_singly(self):
        # Option (a): the resume point stays AT the block whose read stalled,
        # and the next pass reads its channels singly before anything else.
        p, _pub = self._poller(self.ABC[:2] + [_u16("x", 100)],
                               {"max_read_registers": 10},
                               offline_after_fails=1)
        fake = FakeSerial(regs={0: 10, 1: 11}, fail_addrs={100}, max_count=1)
        self.assertEqual(self._window(p, fake), [(4, 0, 2)])
        self.assertEqual(self._window(p, fake)[:2], [(4, 0, 1), (4, 1, 1)])

    def test_stall_inside_refused_block_does_not_starve_its_tail(self):
        # D2 at channel level: the first channel of a refused block is broken
        # and every failure is a stall (threshold 1).
        p, pub = self._poller(self.ABC, {"max_read_registers": 10},
                              offline_after_fails=1)
        fake = FakeSerial(regs={1: 11, 2: 12}, fail_addrs={0}, max_count=1)
        for _ in range(4):
            self._window(p, fake)
        values = self._values(pub)
        self.assertEqual((values.get("b"), values.get("c")), ("11", "12"), values)

    def test_split_pass_with_a_failing_run_reads_to_the_end(self):
        # Review A3, restated for review B1: a block split and a later run of
        # failures in the same pass. The device answered in that pass, so the
        # pass reads to the end (the tail w is not dropped) and the next pass
        # starts at the first unit — now the block's first single read.
        channels = [_u16("a", 0, "input"), _u16("b", 1, "input"),
                    _u16("y", 100), _u16("z1", 200), _u16("z2", 300),
                    _u16("z3", 400), _u16("w", 500)]
        p, pub = self._poller(channels, {"max_read_registers": 10})
        fake = FakeSerial(regs={0: 10, 1: 11, 100: 1, 500: 5},
                          fail_addrs={200, 300, 400}, max_count=1)
        tail = [(3, 100, 1), (3, 200, 1), (3, 300, 1), (3, 400, 1), (3, 500, 1)]
        swept = [(4, 0, 2), (4, 0, 1), (4, 1, 1)]
        # Pass 1: block refused (count 1), the failing run crosses the
        # threshold, the pass goes on to w.
        self.assertEqual(self._window(p, fake), swept + tail)
        # Pass 2: refused again → split, and still read to the end.
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertEqual(self._window(p, fake), swept + tail)
        pub.pub_control.reset_mock()
        self.assertEqual(self._window(p, fake),
                         [(4, 0, 1), (4, 1, 1)] + tail)
        self.assertEqual(self._values(pub).get("w"), "5")
        self.assertTrue(p._online)

    def test_dead_grouped_device_costs_one_read_per_window(self):
        p, _pub = self._poller([_u16(f"c{i}", i, "input") for i in range(6)],
                               {"max_read_registers": 10}, offline_after_fails=3)
        fake = FakeSerial(dead=True)
        self.assertEqual(len(self._window(p, fake)), 3)
        for _ in range(8):
            self.assertEqual(len(self._window(p, fake)), 1)
        self.assertFalse(p._online)

    def test_attempt_with_nothing_read_resets_the_split_count(self):
        # A1: the split needs two CONSECUTIVE block attempts each followed by
        # at least one channel read singly; an attempt during which the device
        # was silent (nothing read) resets the count.
        p, _pub = self._poller(self.ABC, {"max_read_registers": 10})
        fake = FakeSerial(regs={0: 10, 1: 11, 2: 12}, max_count=1)
        with self.assertLogs(LOGGER, level="WARNING") as cm:
            self._window(p, fake)                   # refused, singles read: 1
            fake.dead = True
            # The last pass answered, so this one is read in full: refused,
            # and the sweep reads nothing — the count restarts.
            self._window(p, fake)
            self._window(p, fake)                   # silent twice: stops at
            fake.dead = False                       # the block, sweep pending
            self._window(p, fake)                   # sweep reads: count 1
            p_before = self._window(p, fake)        # refused again: 2, split
            p_after = self._window(p, fake)
        self.assertEqual(p_before[0], (4, 0, 3), p_before)   # still a block
        self.assertEqual(p_after, [(4, 0, 1), (4, 1, 1), (4, 2, 1)], p_after)
        self.assertEqual(len([ln for ln in cm.output if "singly" in ln]), 1,
                         cm.output)


class TestInvalidGroupingValues(_Base):
    def test_invalid_grouping_values_warn_and_default(self):
        channels = [_u16("a", 0, "input"), _u16("b", 2, "input")]
        for bad in ("x", -1, True):
            with self.subTest(key="max_read_registers", value=bad):
                with self.assertLogs(LOGGER, level="WARNING") as cm:
                    p, _pub = self._poller(channels,
                                           {"max_read_registers": bad,
                                            "max_reg_hole": 5})
                self.assertTrue(any("max_read_registers" in ln and "invalid" in ln
                                    for ln in cm.output), cm.output)
                fake = FakeSerial()
                self._pass(p, fake)
                self.assertEqual(fake.reads, [(4, 0, 1), (4, 2, 1)])
            for key in ("max_reg_hole", "max_bit_hole"):
                with self.subTest(key=key, value=bad):
                    with self.assertLogs(LOGGER, level="WARNING") as cm:
                        p, _pub = self._poller(
                            channels, {"max_read_registers": 10, key: bad})
                    self.assertTrue(any(key in ln and "invalid" in ln
                                        for ln in cm.output), cm.output)
                    fake = FakeSerial()
                    self._pass(p, fake)
                    # Hole defaults to 0: the 1-register gap is not bridged.
                    self.assertEqual(fake.reads, [(4, 0, 1), (4, 2, 1)])


class TestGroupOverTcp(_Base):
    """The same split over the real Modbus TCP client and an MBAP server that
    answers the block with exception 02 (illegal data address)."""

    def test_group_over_tcp_split_on_exception(self):
        bank = Bank(regs={("i", a): 100 + a for a in range(4)})
        bank.reject[(4, 0, 4)] = 2
        srv = FakeMbapServer(bank)
        self.addCleanup(srv.stop)
        client = bridge_tcp.ModbusTcpClient("127.0.0.1", srv.port, timeout=0.5,
                                            refuse_self=False)
        self.addCleanup(client.close)
        pool = mock.patch.dict(bridge_tcp._tcp_pool,
                               {("192.0.2.10", 502): client}, clear=True)
        pool.start()
        self.addCleanup(pool.stop)
        p, pub = self._poller(
            [_u16(n, a, "input") for n, a in (("a", 0), ("b", 1), ("c", 2), ("d", 3))],
            {"max_read_registers": 10},
            transport="tcp", host="192.0.2.10", tcp_port=502, poll_s=0)

        def requests():
            out = []
            for _tid, _unit, fc, pdu in list(srv.requests):
                start, count = struct.unpack(">HH", pdu[1:5])
                out.append((fc, start, count))
            srv.requests.clear()
            return out

        singles = [(4, 0, 1), (4, 1, 1), (4, 2, 1), (4, 3, 1)]
        with self.assertLogs(LOGGER, level="WARNING"), \
                mock.patch.object(bridge.DeviceLiveCache, "flush_file"):
            for _ in range(2):
                p._t_poll = -1e9
                p.poll_io()
                self.assertEqual(requests(), [(4, 0, 4)] + singles)
            p._t_poll = -1e9
            p.poll_io()
        self.assertEqual(requests(), singles)
        self.assertEqual(self._values(pub),
                         {"a": "100", "b": "101", "c": "102", "d": "103"})
        self.assertTrue(p._online)


if __name__ == "__main__":
    unittest.main()
