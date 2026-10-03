"""Profile range access, an empty window, and the 2 s budget."""

import unittest
from datetime import datetime

from sa02m_spodes.axdr import TAG_ARRAY, Data, array, enc_len, octet, structure, u32
from sa02m_spodes.hdlc import LLC_RESPONSE, UA, build_frame, i_control, ns_of, parse_frame, SNRM
from sa02m_spodes.profiles import (
    PROFILE_BUDGET_S,
    pack_datetime,
    read_load_profile,
    rows_from_buffer,
)
from sa02m_spodes.session import ClientSession
from sa02m_spodes.xdlms import encode

T0 = datetime(2026, 10, 1, 0, 0, 0)
T1 = datetime(2026, 10, 1, 1, 0, 0)


def _aare() -> bytes:
    body = bytes((0xA2, 0x03, 0x02, 0x01, 0x00))
    return bytes((0x61, len(body))) + body


class Rec:
    def __init__(self, apdus):
        self.apdus = list(apdus)
        self.ns = 0
        self.timeouts = []

    def exchange(self, data, timeout_s):
        self.timeouts.append(timeout_s)
        frame, _ = parse_frame(data)
        if frame.control == SNRM:
            return build_frame(frame.src, frame.dest, UA)
        apdu = self.apdus.pop(0)
        nr = (ns_of(frame.control) + 1) & 7
        ctrl = i_control(self.ns, nr, 1)
        self.ns = (self.ns + 1) & 7
        return build_frame(frame.src, frame.dest, ctrl, LLC_RESPONSE + apdu)


def _get_data(value: Data) -> bytes:
    return bytes((0xC4, 0x01, 0xC1, 0x00)) + encode(value)


class ProfileTest(unittest.TestCase):
    def test_rows_carry_a_timestamp(self):
        stamp = pack_datetime(T0)
        buf = array([structure([octet(stamp), u32(10)])])
        rows = rows_from_buffer(buf)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], T0)
        self.assertEqual(rows[0]["values"], [10])

    def test_empty_window_is_an_empty_list(self):
        self.assertEqual(rows_from_buffer(Data(TAG_ARRAY, [])), [])
        meter = Rec([_aare(), _get_data(array([]))])
        session = ClientSession(meter, timeout_s=5.0)
        session.associate()
        rows = read_load_profile(session, T0, T1)
        self.assertEqual(rows, [])

    def test_budget_caps_the_exchange(self):
        meter = Rec([_aare(), _get_data(array([]))])
        session = ClientSession(meter, timeout_s=5.0)
        session.associate()
        before = len(meter.timeouts)
        read_load_profile(session, T0, T1, timeout_s=30.0)
        used = meter.timeouts[before:]
        self.assertTrue(used)
        self.assertTrue(all(t <= PROFILE_BUDGET_S for t in used))

    def test_block_transfer_keeps_the_session(self):
        stamp = pack_datetime(T0)
        full = encode(array([structure([octet(stamp), u32(7)])]))
        def block(last, number, raw):
            apdu = bytes((0xC4, 0x02, 0xC1, 1 if last else 0))
            apdu += number.to_bytes(4, "big")
            return apdu + bytes((0x00,)) + enc_len(len(raw)) + raw
        meter = Rec([
            _aare(),
            block(False, 1, full[:4]),
            block(True, 2, full[4:]),
        ])
        session = ClientSession(meter, timeout_s=5.0)
        session.associate()
        rows = read_load_profile(session, T0, T1)
        self.assertTrue(session.associated)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["values"], [7])


if __name__ == "__main__":
    unittest.main()
