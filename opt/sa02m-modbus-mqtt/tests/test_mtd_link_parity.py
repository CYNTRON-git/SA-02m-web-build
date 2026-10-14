#!/usr/bin/env python3
"""MTDx62-MB recognition: the MQTT scan and the flasher scan give equal verdicts.

Two trees, two implementations of one rule (they deploy separately and share
no import path): the bridge's `mqtt_bus_scan.detect_type` (`mtdx62_match` on
holding 7..9 plus input 0 / input 3) and the flasher's
`opt/sa02m-flasher/sa02m_flasher/mtd_link.py`. This suite feeds both the same
register banks and requires the same verdict on every case — a behaviour
parity, not a byte copy. It lives in the BRIDGE suite because mqtt_bus_scan
needs pyserial at import (stubbed here when absent); mtd_link has zero imports
and is loaded by file path.

Non-vacuous by construction: a missing file is a failure (not a skip), the case
table must hold at least 10 cases with both verdicts present, and every case
must actually be evaluated on both sides.

Banks: two real dumps (PC COM6 9600 addr 1; bench 1.135 COM3 19200 addr 20,
2026-09-29), an MR-02m 6DO8DI-like bank (input 0 = type code 1, as a live MTD's
presence can read), the Carel uAria input floats, and out-of-range link values.
"""
from __future__ import annotations

import importlib.util
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
MTD_LINK_PATH = BRIDGE_DIR.parent / "sa02m-flasher" / "sa02m_flasher" / "mtd_link.py"
SCAN_PATH = BRIDGE_DIR / "mqtt_bus_scan.py"
sys.path.insert(0, str(BRIDGE_DIR))

try:
    import serial  # noqa: F401
except ImportError:                       # mqtt_bus_scan exits at import without it
    sys.modules["serial"] = types.ModuleType("serial")

REAL_HR = [600, 60, 10, 30, 7, 7, 60, 1, 9600, 0, 2, 0, 600, 1]
REAL_IR = [1, 3471, 148, 1, 1, 260, 0, 0]
BENCH_HR = [600, 60, 10, 30, 7, 7, 60, 20, 19200, 0, 2, 0, 600, 1]
BENCH_IR = [1, 3682, 141, 1, 1, 260, 0, 0]


def _float_words(f):
    hi, lo = struct.unpack(">HH", struct.pack(">f", f))
    return [hi, lo]


UARIA_IR = _float_words(0.0) + _float_words(27.22) + _float_words(43.59) + [0, 0]


def _with(bank, **at):
    out = list(bank)
    for k, v in at.items():
        out[int(k[1:])] = v
    return out


# (label, addr, holding bank, input bank) — a bank is answered only inside its
# length; None = the device answers nothing there (exception / silence).
CASES = [
    ("bench dump 20 @19200", 20, BENCH_HR, BENCH_IR),
    ("pc dump 1 @9600", 1, REAL_HR, REAL_IR),
    ("empty room", 20, BENCH_HR, _with(BENCH_IR, r0=0)),
    ("status 0 self-test", 1, REAL_HR, _with(REAL_IR, r3=0)),
    ("status 6 boundary", 1, REAL_HR, _with(REAL_IR, r3=6)),
    ("address 247", 247, _with(REAL_HR, r7=247), REAL_IR),
    ("baud 57600", 1, _with(REAL_HR, r8=57600), REAL_IR),
    ("parity even", 1, _with(REAL_HR, r9=2), REAL_IR),
    ("HR7 is another address", 21, BENCH_HR, BENCH_IR),
    ("baud 115200 low word", 1, _with(REAL_HR, r8=115200 & 0xFFFF), REAL_IR),
    ("baud 9601", 1, _with(REAL_HR, r8=9601), REAL_IR),
    ("parity code 3", 1, _with(REAL_HR, r9=3), REAL_IR),
    ("presence 2", 1, REAL_HR, _with(REAL_IR, r0=2)),
    ("status 7", 1, REAL_HR, _with(REAL_IR, r3=7)),
    ("holding stops at 8", 1, REAL_HR[:9], REAL_IR),
    ("input stops at 1", 1, REAL_HR, REAL_IR[:1]),
    ("no input bank", 1, REAL_HR, None),
    ("MR 6DO8DI-like", 3, [0, 60, 0, 0, 0, 0, 0, 5, 100, 3], [1, 0, 0, 0]),
    ("Carel uAria, holding refused", 1, [0, 0], UARIA_IR),
    ("Carel uAria behind an MTD-like holding bank", 1, REAL_HR, UARIA_IR),
]


def _load_mtd_link():
    spec = importlib.util.spec_from_file_location("flasher_mtd_link_under_test", MTD_LINK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reader(bank):
    def read(_ser, _addr, start, count=1, **_kw):
        if bank is None:
            return None
        words = bank[start:start + count]
        return list(words) if len(words) == count else None
    return read


class TestMtdLinkParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for path in (MTD_LINK_PATH, SCAN_PATH):
            if not path.is_file():
                raise AssertionError("parity side missing: %s" % path)
        cls.link = _load_mtd_link()
        import mqtt_bus_scan
        cls.scan = mqtt_bus_scan

    def _bridge_verdict(self, addr, hr, ir):
        with mock.patch.object(self.scan, "read_holding", _reader(hr)), \
                mock.patch.object(self.scan, "read_input", _reader(ir)):
            return self.scan.detect_type(None, addr)[0] == "template"

    def _flasher_verdict(self, addr, hr, ir):
        def words(bank, start, count):
            if bank is None:
                return None
            out = bank[start:start + count]
            return out if len(out) == count else None
        return (self.link.link_triple_match(words(hr, 7, 3), addr)
                and self.link.inputs_match(words(ir, 0, 1), words(ir, 3, 1)))

    def test_equal_verdicts_on_every_case(self):
        self.assertGreaterEqual(len(CASES), 10)
        ran, verdicts = 0, set()
        for label, addr, hr, ir in CASES:
            with self.subTest(case=label):
                bridge = self._bridge_verdict(addr, hr, ir)
                flasher = self._flasher_verdict(addr, hr, ir)
                self.assertEqual(bool(flasher), bridge)
                verdicts.add(bridge)
                ran += 1
        self.assertEqual(ran, len(CASES))
        self.assertEqual(verdicts, {True, False}, "both verdicts must occur")

    def test_the_known_verdicts(self):
        """Anchors the table to the real dumps, so two sides agreeing on a
        wrong rule still fails."""
        self.assertTrue(self._bridge_verdict(20, BENCH_HR, BENCH_IR))
        self.assertTrue(self._flasher_verdict(20, BENCH_HR, BENCH_IR))
        self.assertFalse(self._flasher_verdict(1, REAL_HR, UARIA_IR))

    def test_the_link_triple_predicates_agree_directly(self):
        triples = [(20, 19200, 0), (1, 9600, 2), (1, 9600, 3), (2, 9600, 0),
                   (1, 115200 & 0xFFFF, 0), (1, 1200, 1), (1, 57600, 0)]
        for t in triples:
            for addr in (1, 2, 20):
                self.assertEqual(self.link.link_triple_match(list(t), addr),
                                 self.scan.mtdx62_match(list(t), addr), (t, addr))
        for short in (None, [], [1], [1, 9600]):
            self.assertEqual(self.link.link_triple_match(short, 1),
                             self.scan.mtdx62_match(short, 1), short)

    def test_the_baud_sets_are_equal(self):
        self.assertEqual(set(self.link.MTD_BAUDS), set(self.scan._MTD_BAUDS))


if __name__ == "__main__":
    unittest.main()
